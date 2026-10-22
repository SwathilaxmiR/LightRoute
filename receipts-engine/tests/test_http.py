from __future__ import annotations

import json

import httpx
import pytest
import respx

from receipts.cache import SnapshotStore
from receipts.guards import RobotsDisallowed
from receipts.http import (
    CircuitOpen,
    FixtureMissing,
    HttpClient,
    NetworkError,
    QuotaExceeded,
    display_url,
    write_fixture_file,
)


class Sleeps:
    def __init__(self):
        self.calls: list[float] = []

    async def __call__(self, s):
        self.calls.append(s)


@pytest.fixture
def fast(settings):
    s = settings.model_copy(deep=True)
    s.http.per_host_delay_s = 0
    s.http.backoff_base_s = 0.01
    return s


@pytest.fixture
def store(db, tmp_path):
    return SnapshotStore(db, tmp_path / "snaps")


@respx.mock
async def test_retry_on_503_then_success_saves_snapshot(fast, store):
    respx.get("https://site.test/robots.txt").respond(404)
    route = respx.get("https://site.test/about")
    route.side_effect = [httpx.Response(503), httpx.Response(503), httpx.Response(200, text="<html><body>About me</body></html>",
                                                                                   headers={"content-type": "text/html"})]
    sleeps = Sleeps()
    async with HttpClient(fast, store, sleep=sleeps, fixture_mode=False) as c:
        r = await c.get("https://site.test/about")
    assert r.status_code == 200 and route.call_count == 3
    assert r.snap_id and "About me" in store.load_snapshot_text(r.snap_id)
    assert len(sleeps.calls) == 2 and sleeps.calls[1] > sleeps.calls[0]  # exponential


@respx.mock
async def test_retry_after_header_honoured(fast, store):
    route = respx.get("https://api.test/x")
    route.side_effect = [httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200, json={"ok": 1})]
    sleeps = Sleeps()
    async with HttpClient(fast, store, sleep=sleeps, fixture_mode=False) as c:
        r = await c.get("https://api.test/x", kind="api")
    assert r.json() == {"ok": 1} and sleeps.calls == [7.0]


@respx.mock
async def test_retries_exhausted_raises_network_error(fast, store):
    fast.http.circuit_breaker_failures = 100
    respx.get("https://api.test/x").respond(500)
    async with HttpClient(fast, store, sleep=Sleeps(), fixture_mode=False) as c:
        with pytest.raises(NetworkError):
            await c.get("https://api.test/x", kind="api")


@respx.mock
async def test_timeout_retried(fast, store):
    route = respx.get("https://api.test/t")
    route.side_effect = [httpx.ReadTimeout("slow"), httpx.Response(200, json={})]
    async with HttpClient(fast, store, sleep=Sleeps(), fixture_mode=False) as c:
        r = await c.get("https://api.test/t", kind="api")
    assert r.ok and route.call_count == 2


@respx.mock
async def test_robots_denial(fast, store):
    respx.get("https://site.test/robots.txt").respond(200, text="User-agent: *\nDisallow: /private\n")
    page = respx.get("https://site.test/private/page").respond(200, text="secret")
    respx.get("https://site.test/public").respond(200, text="ok")
    async with HttpClient(fast, store, fixture_mode=False) as c:
        with pytest.raises(RobotsDisallowed):
            await c.get("https://site.test/private/page")
        assert (await c.get("https://site.test/public")).ok
    assert page.call_count == 0


@respx.mock
async def test_robots_not_checked_for_api(fast, store):
    robots = respx.get("https://api.test/robots.txt").respond(200, text="User-agent: *\nDisallow: /\n")
    respx.get("https://api.test/v3/channels").respond(200, json={"items": []})
    async with HttpClient(fast, store, fixture_mode=False) as c:
        assert (await c.get("https://api.test/v3/channels", kind="api")).ok
    assert robots.call_count == 0


@respx.mock
async def test_circuit_breaker_opens(fast, store):
    fast.http.retries = 0
    fast.http.circuit_breaker_failures = 3
    route = respx.get("https://down.test/x").respond(502)
    async with HttpClient(fast, store, sleep=Sleeps(), fixture_mode=False) as c:
        for _ in range(3):
            with pytest.raises(NetworkError):
                await c.get("https://down.test/x", kind="api", cache=False)
        with pytest.raises(CircuitOpen):
            await c.get("https://down.test/x", kind="api", cache=False)
    assert route.call_count == 3


@respx.mock
async def test_quota_exceeded_not_retried(fast, store):
    body = {"error": {"code": 403, "errors": [{"reason": "quotaExceeded"}]}}
    route = respx.get("https://www.googleapis.com/youtube/v3/search").respond(403, json=body)
    async with HttpClient(fast, store, sleep=Sleeps(), fixture_mode=False) as c:
        with pytest.raises(QuotaExceeded):
            await c.get("https://www.googleapis.com/youtube/v3/search", params={"q": "x", "key": "k"}, kind="api")
    assert route.call_count == 1


@respx.mock
async def test_cache_reuses_fresh_snapshot(fast, store):
    route = respx.get("https://api.test/c").respond(200, json={"n": 1})
    async with HttpClient(fast, store, fixture_mode=False) as c:
        a = await c.get("https://api.test/c", kind="api")
        b = await c.get("https://api.test/c", kind="api")
    assert route.call_count == 1 and b.from_cache and a.snap_id == b.snap_id


async def test_fixture_replay_never_touches_network(fast, store, tmp_path):
    fx = tmp_path / "fx"
    write_fixture_file(fx, "https://api.test/v", {"id": "1", "key": "SECRET"}, json.dumps({"v": 1}))
    with respx.mock(assert_all_called=False) as m:
        net = m.route().respond(500)
        async with HttpClient(fast, store, fixture_mode=True, fixtures_dir=fx) as c:
            r = await c.get("https://api.test/v", params={"key": "OTHER", "id": "1"}, kind="api")
            with pytest.raises(FixtureMissing):
                await c.get("https://api.test/v", params={"id": "2"}, kind="api")
        assert net.call_count == 0
    assert r.json() == {"v": 1} and r.from_fixture
    assert store.get(r.snap_id)["data_origin"] == "synthetic"  # hand-built fixture
    assert "SECRET" not in r.url and "key=" not in r.url


@respx.mock
async def test_record_mode_writes_fixture_without_key(fast, store, tmp_path):
    fx = tmp_path / "fx"
    respx.get("https://api.test/r").respond(200, json={"a": 1})
    async with HttpClient(fast, store, fixture_mode=False, record=True, fixtures_dir=fx) as c:
        await c.get("https://api.test/r", params={"key": "MYKEY123", "q": "z"}, kind="api")
    files = list((fx / "http").glob("*.json"))
    assert len(files) == 1 and "MYKEY123" not in files[0].read_text()
    async with HttpClient(fast, store, fixture_mode=True, fixtures_dir=fx) as c:
        r = await c.get("https://api.test/r", params={"key": "whatever", "q": "z"}, kind="api")
    assert r.json() == {"a": 1}
    assert store.get(r.snap_id)["data_origin"] == "fixture"  # recorded from a real response


def test_display_url_strips_secrets_and_sorts():
    assert display_url("https://a.test/x", {"b": 2, "key": "k", "a": 1}) == "https://a.test/x?a=1&b=2"
