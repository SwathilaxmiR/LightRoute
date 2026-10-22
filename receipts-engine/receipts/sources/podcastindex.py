"""Podcast Index API client (https://podcastindex-org.github.io/docs-api/)."""
from __future__ import annotations

import hashlib
import os
import time

from pydantic import BaseModel, Field

from receipts.http import HttpClient, SourceAuthError
from receipts.models import ContentItem
from receipts.util.dates import parse_dt

BASE = "https://api.podcastindex.org/api/1.0"


class PIFeed(BaseModel):
    feed_id: int
    title: str = ""
    url: str = ""
    link: str = ""
    description: str = ""
    author: str = ""
    owner_name: str = ""
    language: str = ""
    last_update: int | None = None
    newest_item: int | None = None
    episode_count: int | None = None
    itunes_id: int | None = None
    categories: list[str] = Field(default_factory=list)
    dead: bool = False
    snap_id: int | None = None


class PodcastIndexAPI:
    def __init__(self, http: HttpClient, key: str | None = None, secret: str | None = None):
        self.http = http
        self.key = key if key is not None else os.environ.get("PODCASTINDEX_KEY", "")
        self.secret = secret if secret is not None else os.environ.get("PODCASTINDEX_SECRET", "")

    def _headers(self) -> dict[str, str]:
        if not (self.key and self.secret):
            raise SourceAuthError("PODCASTINDEX_KEY / PODCASTINDEX_SECRET are not set (put them in .env, or use --fixtures)")
        date = str(int(time.time()))
        auth = hashlib.sha1((self.key + self.secret + date).encode()).hexdigest()
        return {"X-Auth-Date": date, "X-Auth-Key": self.key, "Authorization": auth,
                "User-Agent": self.http.s.project.user_agent}

    async def _get(self, path: str, params: dict) -> tuple[dict, int | None]:
        headers = {} if self.http.fixture_mode else self._headers()
        r = await self.http.get(f"{BASE}/{path}", params=params, headers=headers, kind="api", expect="json")
        if r.status_code in (401, 403):
            raise SourceAuthError(f"Podcast Index rejected the key ({r.status_code})")
        if not r.ok:
            return {}, None
        return r.json(), r.snap_id

    @staticmethod
    def _feed(f: dict, sid: int | None) -> PIFeed:
        cats = f.get("categories") or {}
        return PIFeed(
            feed_id=int(f.get("id") or f.get("feedId")), title=f.get("title") or "", url=f.get("url") or "",
            link=f.get("link") or "", description=f.get("description") or "", author=f.get("author") or "",
            owner_name=f.get("ownerName") or "", language=f.get("language") or "",
            last_update=f.get("lastUpdateTime"), newest_item=f.get("newestItemPublishTime") or f.get("newestItemPublishedTime"),
            episode_count=f.get("episodeCount"), itunes_id=f.get("itunesId") or None,
            categories=list(cats.values()) if isinstance(cats, dict) else list(cats), dead=bool(f.get("dead")),
            snap_id=sid,
        )

    async def search_by_term(self, q: str, max: int = 50) -> list[PIFeed]:
        data, sid = await self._get("search/byterm", {"q": q, "max": max})
        return [self._feed(f, sid) for f in data.get("feeds", []) if f.get("id")]

    async def by_feed_id(self, feed_id: int | str) -> PIFeed | None:
        data, sid = await self._get("podcasts/byfeedid", {"id": feed_id})
        f = data.get("feed")
        return self._feed(f, sid) if f and f.get("id") else None

    async def episodes_by_feed_id(self, feed_id: int | str, max: int = 100) -> list[ContentItem]:
        data, sid = await self._get("episodes/byfeedid", {"id": feed_id, "max": max})
        out = []
        for e in data.get("items", []):
            pub = parse_dt(str(e["datePublished"])) if isinstance(e.get("datePublished"), str) else (
                parse_dt(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(e["datePublished"])))
                if e.get("datePublished") else None)
            if pub is None:
                continue
            out.append(ContentItem(
                platform="podcast", item_id=f"pi:{e['id']}", url=e.get("link") or e.get("enclosureUrl") or "",
                title=e.get("title"), published_at=pub, duration_s=e.get("duration") or None,
                description=e.get("description"), snap_id=sid,
            ))
        return out

    async def by_category(self, cat: str, max: int = 100, lang: str = "en") -> list[PIFeed]:
        # no dedicated category search endpoint; recent/feeds filters by category name (see assumptions.md)
        data, sid = await self._get("recent/feeds", {"cat": cat, "lang": lang, "max": max})
        return [self._feed(f, sid) for f in data.get("feeds", []) if f.get("id")]
