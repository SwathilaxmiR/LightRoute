"""Async HTTP layer (BUILD_SPEC 8.4).

- FIXTURE_MODE=1: replay from <fixtures>/http/<key>.json, never touch the network.
- RECORD_FIXTURES=1: live fetch and also write the fixture file (used by scripts/record_fixtures.py).
- robots.txt (G6) for kind="web"; per-host delay for kind="web"; global semaphore.
- tenacity retry on 429/5xx/timeouts with exponential backoff honouring Retry-After.
- per-host circuit breaker; YouTube quota 403 -> QuotaExceeded (never retried).
- every 2xx body saved as a snapshot; resp.snap_id set.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlencode, urlsplit

import httpx
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt

from receipts.cache import SnapshotStore
from receipts.guards import RobotsDisallowed, robots_allows

SECRET_PARAMS = {"key", "api_key", "apikey", "token", "access_token"}
_RECORD_HEADERS = {"content-type", "retry-after", "last-modified", "etag"}


class FixtureMissing(Exception):
    pass


class CircuitOpen(Exception):
    pass


class QuotaExceeded(Exception):
    pass


class SourceAuthError(Exception):
    """Missing or rejected API key. Stops the run like QuotaExceeded; fix .env then --resume."""


class NetworkError(Exception):
    """Transport failure or retryable status that survived all retries."""

    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status


class _Retryable(Exception):
    def __init__(self, status: int | None, retry_after: float | None = None, msg: str = ""):
        super().__init__(msg or f"retryable status {status}")
        self.status = status
        self.retry_after = retry_after


TRANSIENT = (QuotaExceeded, CircuitOpen, NetworkError, httpx.TransportError)
RUN_STOPPING = (QuotaExceeded, SourceAuthError)


@dataclass
class Response:
    url: str
    status_code: int
    content: bytes
    content_type: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    snap_id: int | None = None
    from_cache: bool = False
    from_fixture: bool = False

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.content)

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


def request_key(url: str, params: dict | None = None) -> str:
    return hashlib.sha256(display_url(url, params).encode()).hexdigest()[:24]


def display_url(url: str, params: dict | None = None) -> str:
    """URL with params sorted and secrets removed; this is the snapshot/fixture identity."""
    url = url.split("#", 1)[0]
    clean = sorted((k, str(v)) for k, v in (params or {}).items() if k.lower() not in SECRET_PARAMS)
    if not clean:
        return url
    return url + ("&" if "?" in url else "?") + urlencode(clean)


def _parse_retry_after(v: str | None) -> float | None:
    if not v:
        return None
    try:
        return max(0.0, float(v))
    except ValueError:
        return None


def is_quota_error(status: int, body: bytes) -> bool:
    if status != 403:
        return False
    try:
        errs = json.loads(body).get("error", {}).get("errors", [])
    except (ValueError, AttributeError):
        return False
    return any(e.get("reason") in ("quotaExceeded", "dailyLimitExceeded") for e in errs)


class HttpClient:
    def __init__(self, settings, store: SnapshotStore | None, *, fixture_mode: bool | None = None,
                 record: bool | None = None, transport: httpx.AsyncBaseTransport | None = None,
                 sleep=asyncio.sleep, fixtures_dir: str | Path | None = None):
        self.s = settings
        self.store = store
        self.fixture_mode = os.environ.get("FIXTURE_MODE") == "1" if fixture_mode is None else fixture_mode
        self.record = os.environ.get("RECORD_FIXTURES") == "1" if record is None else record
        self.fixtures_dir = Path(fixtures_dir or settings.fixtures_dir) / "http"
        self._transport = transport
        self._sleep = sleep
        self._sem = asyncio.Semaphore(settings.http.max_concurrency)
        self._host_locks: dict[str, asyncio.Lock] = {}
        self._host_last: dict[str, float] = {}
        self._failures: dict[str, int] = {}
        self._robots: dict[str, tuple[float, str | None]] = {}
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> HttpClient:
        self._client = httpx.AsyncClient(
            timeout=self.s.http.timeout_s, follow_redirects=True, transport=self._transport,
            headers={"User-Agent": self.s.project.user_agent},
        )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client:
            await self._client.aclose()

    @property
    def origin(self) -> str:
        return "fixture" if self.fixture_mode else "live"

    # ------------------------------------------------------------------ public
    async def get(self, url: str, *, headers: dict | None = None, params: dict | None = None,
                  kind: Literal["web", "api"] = "web", cache: bool = True,
                  expect: Literal["text", "json", "bytes"] = "text", api_units: int = 0,
                  method: str = "GET") -> Response:
        durl = display_url(url, params)
        if self.fixture_mode:
            if kind == "web":
                await self._check_robots(url)
            return self._replay(url, params, durl, api_units)

        if cache and self.store is not None:
            sid = self.store.fresh_snapshot(durl, self.s.http.cache_hours)
            if sid is not None:
                row = self.store.get(sid)
                return Response(durl, row["status_code"] or 200, self.store.load_raw(sid),
                                row["content_type"] or "", snap_id=sid, from_cache=True)

        host = urlsplit(url).hostname or ""
        if self._failures.get(host, 0) >= self.s.http.circuit_breaker_failures:
            raise CircuitOpen(f"circuit open for {host}")
        if kind == "web":
            await self._check_robots(url)

        resp = await self._fetch_with_retry(method, url, headers, params, kind, host)
        if self.record:
            self._write_fixture(url, params, resp)
        if resp.ok and self.store is not None:
            resp.snap_id = self.store.save_snapshot(durl, resp.content, resp.status_code, resp.content_type,
                                                    "live", api_units)
        resp.url = durl
        return resp

    # ------------------------------------------------------------------ internals
    async def _fetch_with_retry(self, method, url, headers, params, kind, host) -> Response:
        base = self.s.http.backoff_base_s

        def wait(rs) -> float:
            exc = rs.outcome.exception()
            ra = getattr(exc, "retry_after", None)
            if ra is not None:
                return min(ra, 120.0)
            return base * (2 ** (rs.attempt_number - 1))

        def should_retry(e: BaseException) -> bool:
            return isinstance(e, (_Retryable, httpx.TimeoutException, httpx.TransportError))

        try:
            async for attempt in AsyncRetrying(stop=stop_after_attempt(self.s.http.retries + 1), wait=wait,
                                               retry=retry_if_exception(should_retry), sleep=self._sleep,
                                               reraise=True):
                with attempt:
                    if self._failures.get(host, 0) >= self.s.http.circuit_breaker_failures:
                        raise CircuitOpen(f"circuit open for {host}")
                    try:
                        resp = await self._once(method, url, headers, params, kind, host)
                    except (httpx.TimeoutException, httpx.TransportError):
                        self._failures[host] = self._failures.get(host, 0) + 1
                        raise
                    if resp.status_code == 429 or resp.status_code >= 500:
                        self._failures[host] = self._failures.get(host, 0) + 1
                        raise _Retryable(resp.status_code, _parse_retry_after(resp.headers.get("retry-after")))
                    if is_quota_error(resp.status_code, resp.content):
                        raise QuotaExceeded(f"YouTube quota exceeded ({url.split('?')[0]})")
                    self._failures[host] = 0
                    return resp
        except _Retryable as e:
            raise NetworkError(f"{url}: status {e.status} after retries", e.status) from e
        except (httpx.TimeoutException, httpx.TransportError) as e:
            raise NetworkError(f"{url}: {type(e).__name__}") from e
        raise NetworkError(f"{url}: exhausted")  # pragma: no cover

    async def _once(self, method, url, headers, params, kind, host) -> Response:
        assert self._client is not None, "use `async with HttpClient(...)`"
        async with self._sem:
            if kind == "web":
                await self._host_delay(host)
            r = await self._client.request(method, url, headers=headers, params=params)
        return Response(str(r.url), r.status_code, r.content, r.headers.get("content-type", ""),
                        {k.lower(): v for k, v in r.headers.items()})

    async def _host_delay(self, host: str) -> None:
        lock = self._host_locks.setdefault(host, asyncio.Lock())
        async with lock:
            last = self._host_last.get(host)
            delay = self.s.http.per_host_delay_s
            if last is not None and delay > 0:
                wait = delay - (time.monotonic() - last)
                if wait > 0:
                    await self._sleep(wait)
            self._host_last[host] = time.monotonic()

    async def _check_robots(self, url: str) -> None:
        p = urlsplit(url)
        origin = f"{p.scheme}://{p.netloc}"
        cached = self._robots.get(origin)
        if cached is None or time.monotonic() - cached[0] > 86400:
            txt: str | None
            try:
                if self.fixture_mode:
                    try:
                        r = self._replay(origin + "/robots.txt", None, origin + "/robots.txt", 0, save=False)
                    except FixtureMissing:
                        r = Response(origin + "/robots.txt", 404, b"")
                else:
                    r = await self._once("GET", origin + "/robots.txt", None, None, "web", p.hostname or "")
                    if self.record:
                        self._write_fixture(origin + "/robots.txt", None, r)
                if r.ok:
                    txt = r.text
                elif r.status_code >= 500:
                    txt = "User-agent: *\nDisallow: /\n"  # RFC 9309: server error => assume disallow
                else:
                    txt = None
            except (httpx.TimeoutException, httpx.TransportError):
                txt = "User-agent: *\nDisallow: /\n"
            cached = (time.monotonic(), txt)
            self._robots[origin] = cached
        if not robots_allows(cached[1], url, self.s.project.user_agent):
            raise RobotsDisallowed(f"robots.txt disallows {url}")

    # ------------------------------------------------------------------ fixtures
    def _fixture_path(self, url: str, params: dict | None) -> Path:
        return self.fixtures_dir / f"{request_key(url, params)}.json"

    def _write_fixture(self, url: str, params: dict | None, resp: Response) -> None:
        self.fixtures_dir.mkdir(parents=True, exist_ok=True)
        try:
            body, enc = resp.content.decode("utf-8"), "utf-8"
        except UnicodeDecodeError:
            body, enc = base64.b64encode(resp.content).decode(), "base64"
        rec = {
            "origin": "recorded",
            "url": display_url(url, params), "status": resp.status_code, "content_type": resp.content_type,
            "headers": {k: v for k, v in resp.headers.items() if k in _RECORD_HEADERS},
            "encoding": enc, "body": body,
        }
        self._fixture_path(url, params).write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")

    def _replay(self, url: str, params: dict | None, durl: str, api_units: int, save: bool = True) -> Response:
        path = self._fixture_path(url, params)
        if not path.exists():
            raise FixtureMissing(f"no fixture for {durl} ({path.name})")
        rec = json.loads(path.read_text(encoding="utf-8"))
        body = base64.b64decode(rec["body"]) if rec.get("encoding") == "base64" else rec["body"].encode("utf-8")
        status = rec.get("status", 200)
        if is_quota_error(status, body):
            raise QuotaExceeded(f"YouTube quota exceeded (fixture {durl})")
        resp = Response(durl, status, body, rec.get("content_type", ""), rec.get("headers", {}), from_fixture=True)
        origin = "synthetic" if rec.get("origin") == "synthetic" else "fixture"
        if save and resp.ok and self.store is not None:
            resp.snap_id = self.store.save_snapshot(durl, body, status, resp.content_type, origin, api_units)
        return resp


def write_fixture_file(fixtures_dir: str | Path, url: str, params: dict | None, body: bytes | str,
                       status: int = 200, content_type: str = "application/json", origin: str = "synthetic") -> Path:
    """Write one replay fixture. Hand-built fixtures are synthetic by default (rule 0.3)."""
    d = Path(fixtures_dir) / "http"
    d.mkdir(parents=True, exist_ok=True)
    b = body.encode() if isinstance(body, str) else body
    rec = {"origin": origin, "url": display_url(url, params), "status": status, "content_type": content_type,
           "headers": {}, "encoding": "utf-8", "body": b.decode("utf-8")}
    p = d / f"{request_key(url, params)}.json"
    p.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    return p
