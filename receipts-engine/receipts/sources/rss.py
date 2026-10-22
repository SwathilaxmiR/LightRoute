"""RSS/podcast feed parsing with feedparser."""
from __future__ import annotations

import hashlib
import re
from calendar import timegm
from datetime import UTC, datetime

import feedparser
from pydantic import BaseModel

from receipts.http import HttpClient
from receipts.models import ContentItem
from receipts.util.dates import parse_hms
from receipts.util.text import visible_text

_ITEM = re.compile(r"<item[\s>].*?</item>", re.DOTALL | re.IGNORECASE)


class FeedMeta(BaseModel):
    url: str
    title: str = ""
    link: str = ""
    language: str = ""
    description: str = ""
    author: str = ""
    owner_name: str = ""
    owner_email: str = ""            # unverified until corroborated (G3, 8.10 s6)
    episode_count: int = 0
    transcript_items: int = 0
    snap_id: int | None = None


def parse_feed_bytes(url: str, body: bytes, snap_id: int | None = None) -> tuple[FeedMeta, list[ContentItem]]:
    fp = feedparser.parse(body)
    f = fp.feed
    owner = f.get("publisher_detail") or {}
    raw = body.decode("utf-8", errors="replace")
    blocks = _ITEM.findall(raw)
    items: list[ContentItem] = []
    transcripts = 0
    for idx, e in enumerate(fp.entries):
        t = e.get("published_parsed") or e.get("updated_parsed")
        if not t:
            continue
        pub = datetime.fromtimestamp(timegm(t), tz=UTC)
        enc = next((ln.get("href") for ln in e.get("links", []) if ln.get("rel") == "enclosure"), None)
        link = e.get("link") or enc or url
        guid = e.get("id") or link
        has_tx = idx < len(blocks) and "podcast:transcript" in blocks[idx].lower()
        transcripts += has_tx
        desc = e.get("summary") or ""
        items.append(ContentItem(
            platform="podcast", item_id="rss:" + hashlib.sha256(guid.encode()).hexdigest()[:16], url=link,
            title=e.get("title"), published_at=pub, duration_s=parse_hms(e.get("itunes_duration")),
            has_captions=has_tx, description=visible_text(desc) if "<" in desc else desc, snap_id=snap_id,
        ))
    meta = FeedMeta(
        url=url, title=f.get("title", ""), link=f.get("link", ""), language=f.get("language", ""),
        description=visible_text(f.get("summary", "") or f.get("subtitle", "")) if f.get("summary") else f.get("subtitle", ""),
        author=f.get("author", ""), owner_name=owner.get("name", ""), owner_email=owner.get("email", ""),
        episode_count=len(fp.entries), transcript_items=transcripts, snap_id=snap_id,
    )
    return meta, items


async def parse_feed(http: HttpClient, url: str) -> tuple[FeedMeta, list[ContentItem]] | None:
    r = await http.get(url, kind="web", expect="bytes")
    if not r.ok:
        return None
    return parse_feed_bytes(url, r.content, r.snap_id)
