"""YouTube Data API v3 client with a persisted daily quota ledger (BUILD_SPEC 8.7)."""
from __future__ import annotations

import os
from typing import Any

from pydantic import BaseModel, Field

from receipts.db import DB
from receipts.http import HttpClient, QuotaExceeded, SourceAuthError, display_url
from receipts.models import ContentItem
from receipts.util.dates import parse_dt, parse_iso_duration, utcnow
from receipts.util.urls import youtube_video_url

BASE = "https://www.googleapis.com/youtube/v3"
VIDEO_PARTS_FULL = "snippet,contentDetails,statistics,status,paidProductPlacementDetails"
VIDEO_PARTS_BASIC = "snippet,contentDetails,statistics,status"


class QuotaLedger:
    """Units spent per UTC date, stored in meta as yt_units:<YYYY-MM-DD>."""

    def __init__(self, db: DB, settings):
        self.db = db
        self.q = settings.quota
        self.run_units = 0

    @property
    def key(self) -> str:
        return f"yt_units:{utcnow().strftime('%Y-%m-%d')}"

    @property
    def spent(self) -> int:
        return int(self.db.meta_get(self.key, "0") or 0)

    @property
    def limit(self) -> float:
        return self.q.youtube_daily_units * self.q.youtube_stop_at_fraction

    @property
    def remaining(self) -> int:
        return max(0, int(self.limit - self.spent))

    def cost(self, kind: str, n: int = 1) -> int:
        return getattr(self.q.youtube_cost, kind) * n

    def check(self, units: int) -> None:
        if self.spent >= self.limit or self.spent + units > self.limit:
            raise QuotaExceeded(f"local YouTube quota ledger at {self.spent}/{int(self.limit)} units "
                                f"(stop at {self.q.youtube_stop_at_fraction:.0%} of {self.q.youtube_daily_units})")

    def record(self, units: int) -> None:
        self.db.meta_set(self.key, str(self.spent + units))
        self.run_units += units

    def spend(self, kind: str, n: int = 1) -> int:
        c = self.cost(kind, n)
        self.check(c)
        self.record(c)
        return c

    def mark_exhausted(self) -> None:
        """API said quotaExceeded: stop spending for the rest of the UTC day."""
        self.db.meta_set(self.key, str(int(self.q.youtube_daily_units)))


class YTChannel(BaseModel):
    channel_id: str
    title: str
    description: str = ""
    country: str | None = None
    custom_url: str | None = None
    subscribers: int | None = None
    hidden_subscribers: bool = False
    video_count: int | None = None
    uploads_playlist: str | None = None
    featured_channels: list[str] = Field(default_factory=list)
    keywords: str = ""
    snap_id: int | None = None


class YTUpload(BaseModel):
    video_id: str
    published_at: str | None = None


class YouTubeAPI:
    def __init__(self, http: HttpClient, db: DB, settings, api_key: str | None = None):
        self.http = http
        self.s = settings
        self.key = api_key if api_key is not None else os.environ.get("YOUTUBE_API_KEY", "")
        self.ledger = QuotaLedger(db, settings)
        self._video_parts = VIDEO_PARTS_FULL
        self.paid_placement_supported: bool | None = None

    async def _get(self, endpoint: str, params: dict, kind: str) -> tuple[dict, int | None]:
        if not self.key and not self.http.fixture_mode:
            raise MissingKey("YOUTUBE_API_KEY is not set (put it in .env, or use --fixtures)")
        url = f"{BASE}/{endpoint}"
        units = self.ledger.cost(kind)
        cached = (not self.http.fixture_mode and self.http.store is not None
                  and self.http.store.fresh_snapshot(display_url(url, params), self.s.http.cache_hours))
        if not cached and not self.http.fixture_mode:  # replays spend no real quota
            self.ledger.check(units)
        try:
            r = await self.http.get(url, params={**params, "key": self.key}, kind="api", expect="json",
                                    api_units=units)
        except QuotaExceeded:
            self.ledger.mark_exhausted()
            raise
        if not r.from_cache:
            self.ledger.record(units)
        if not r.ok:
            if r.status_code in (400, 401, 403) and any(
                    m in r.text for m in ("keyInvalid", "API key not valid", "API_KEY_INVALID", "accessNotConfigured")):
                raise SourceAuthError(f"YouTube rejected the API key ({r.status_code}); check YOUTUBE_API_KEY")
            raise YouTubeError(r.status_code, r.text[:300])
        return r.json(), r.snap_id

    # ------------------------------------------------------------------ search
    async def search_channels(self, query: str, max_results: int = 50, page_token: str | None = None
                              ) -> tuple[list[str], str | None]:
        p = {"part": "snippet", "type": "channel", "q": query, "maxResults": max_results, "relevanceLanguage": "en"}
        if page_token:
            p["pageToken"] = page_token
        data, _ = await self._get("search", p, "search_list")
        ids = [it["id"]["channelId"] for it in data.get("items", []) if it.get("id", {}).get("channelId")]
        return ids, data.get("nextPageToken")

    async def search_videos(self, query: str, max_results: int = 50, page_token: str | None = None
                            ) -> tuple[list[str], list[str], str | None]:
        p = {"part": "snippet", "type": "video", "q": query, "maxResults": max_results, "relevanceLanguage": "en"}
        if page_token:
            p["pageToken"] = page_token
        data, _ = await self._get("search", p, "search_list")
        vids, chans = [], []
        for it in data.get("items", []):
            if it.get("id", {}).get("videoId"):
                vids.append(it["id"]["videoId"])
            cid = it.get("snippet", {}).get("channelId")
            if cid and cid not in chans:
                chans.append(cid)
        return vids, chans, data.get("nextPageToken")

    # ------------------------------------------------------------------ channels
    async def channels(self, ids: list[str]) -> list[YTChannel]:
        out: list[YTChannel] = []
        for i in range(0, len(ids), 50):
            batch = ids[i:i + 50]
            data, sid = await self._get("channels", {
                "part": "snippet,statistics,brandingSettings,contentDetails", "id": ",".join(batch),
                "maxResults": 50}, "channels_list")
            out += [self._channel(it, sid) for it in data.get("items", [])]
        return out

    async def resolve_handle(self, handle: str) -> YTChannel | None:
        h = handle if handle.startswith("@") else "@" + handle
        data, sid = await self._get("channels", {
            "part": "snippet,statistics,brandingSettings,contentDetails", "forHandle": h}, "channels_list")
        items = data.get("items", [])
        return self._channel(items[0], sid) if items else None

    @staticmethod
    def _channel(it: dict, sid: int | None) -> YTChannel:
        sn, st = it.get("snippet", {}), it.get("statistics", {})
        br = it.get("brandingSettings", {}).get("channel", {})
        hidden = bool(st.get("hiddenSubscriberCount"))
        subs = None if hidden or "subscriberCount" not in st else int(st["subscriberCount"])
        return YTChannel(
            channel_id=it["id"], title=sn.get("title", ""), description=sn.get("description", ""),
            country=sn.get("country") or br.get("country"), custom_url=sn.get("customUrl"),
            subscribers=subs, hidden_subscribers=hidden,
            video_count=int(st["videoCount"]) if st.get("videoCount") is not None else None,
            uploads_playlist=it.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads"),
            featured_channels=list(br.get("featuredChannelsUrls", []) or []),
            keywords=br.get("keywords", "") or "", snap_id=sid,
        )

    # ------------------------------------------------------------------ uploads/videos
    async def uploads(self, playlist_id: str, max_items: int = 50) -> list[YTUpload]:
        data, _ = await self._get("playlistItems", {
            "part": "contentDetails", "playlistId": playlist_id, "maxResults": min(50, max_items)},
            "playlist_items_list")
        return [YTUpload(video_id=it["contentDetails"]["videoId"],
                         published_at=it["contentDetails"].get("videoPublishedAt"))
                for it in data.get("items", []) if it.get("contentDetails", {}).get("videoId")]

    async def videos(self, ids: list[str]) -> list[ContentItem]:
        out: list[ContentItem] = []
        for i in range(0, len(ids), 50):
            batch = ids[i:i + 50]
            try:
                data, sid = await self._get("videos", {"part": self._video_parts, "id": ",".join(batch),
                                                       "maxResults": 50}, "videos_list")
                if self._video_parts == VIDEO_PARTS_FULL:
                    self.paid_placement_supported = True
            except YouTubeError as e:
                if e.status == 400 and self._video_parts == VIDEO_PARTS_FULL:
                    # part rejected: fall back and record paid_placement=None (see docs/assumptions.md)
                    self._video_parts = VIDEO_PARTS_BASIC
                    self.paid_placement_supported = False
                    data, sid = await self._get("videos", {"part": self._video_parts, "id": ",".join(batch),
                                                           "maxResults": 50}, "videos_list")
                else:
                    raise
            out += [v for v in (self._video(it, sid) for it in data.get("items", [])) if v]
        return out

    def _video(self, it: dict, sid: int | None) -> ContentItem | None:
        sn, cd = it.get("snippet", {}), it.get("contentDetails", {})
        pub = parse_dt(sn.get("publishedAt"))
        if pub is None:
            return None
        status = it.get("status", {})
        if status.get("privacyStatus") not in (None, "public"):
            return None
        dur = parse_iso_duration(cd.get("duration", ""))
        live = sn.get("liveBroadcastContent") in ("live", "upcoming")
        cap = cd.get("caption")
        ppd = it.get("paidProductPlacementDetails")
        paid = None if ppd is None else bool(ppd.get("hasPaidProductPlacement"))
        return ContentItem(
            platform="youtube", item_id=it["id"], url=youtube_video_url(it["id"]), title=sn.get("title"),
            published_at=pub, duration_s=None if live else dur,
            is_short=bool(dur and dur <= self.s.thresholds.short_max_seconds),
            has_captions=None if cap is None else cap == "true", paid_placement=paid,
            description=sn.get("description"), snap_id=sid,
        )

    # ------------------------------------------------------------------ responsiveness
    async def comment_reply_rate(self, channel_id: str, video_ids: list[str], sample: int = 20) -> float | None:
        total = replied = 0
        for vid in video_ids[:3]:
            try:
                data, _ = await self._get("commentThreads", {"part": "snippet,replies", "videoId": vid,
                                                             "maxResults": sample, "order": "time"},
                                          "comment_threads_list")
            except YouTubeError:  # comments disabled -> 403 commentsDisabled
                continue
            for th in data.get("items", []):
                total += 1
                reps = th.get("replies", {}).get("comments", [])
                if any(c.get("snippet", {}).get("authorChannelId", {}).get("value") == channel_id for c in reps):
                    replied += 1
        return None if total == 0 else replied / total


class MissingKey(SourceAuthError):
    pass


class YouTubeError(Exception):
    def __init__(self, status: int, body: Any):
        super().__init__(f"YouTube API {status}: {body}")
        self.status = status
