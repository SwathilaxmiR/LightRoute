"""Creator-owned text sources with their snapshot ids, used by every text-based check."""
from __future__ import annotations

from dataclasses import dataclass

from receipts.models import CollectedCreator


@dataclass
class TextSource:
    url: str
    snap_id: int | None
    text: str
    kind: str          # site | yt_description | feed_description | item_description


def creator_texts(c: CollectedCreator, include_items: bool = True, item_limit: int = 15) -> list[TextSource]:
    out: list[TextSource] = []
    yt = c.profile.get("youtube")
    if yt and yt.get("description"):
        out.append(TextSource(yt["url"], yt.get("snap_id"), yt["description"], "yt_description"))
    fm = c.feed_meta or {}
    if fm.get("description"):
        out.append(TextSource(fm.get("url", ""), fm.get("snap_id"), fm["description"], "feed_description"))
    for url, (sid, text) in c.website_pages.items():
        out.append(TextSource(url, sid, text, "site"))
    if include_items:
        recent = sorted(c.items, key=lambda i: i.published_at, reverse=True)[:item_limit]
        for it in recent:
            if it.description:
                out.append(TextSource(it.url, it.snap_id, it.description, "item_description"))
    return out
