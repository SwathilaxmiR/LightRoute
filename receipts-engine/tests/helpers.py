"""Builders for synthetic CollectedCreator objects backed by real snapshot rows (tests only)."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from receipts.cache import SnapshotStore
from receipts.models import Candidate, CollectedCreator, ContentItem

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


class FakeLLM:
    """Returns canned claims per prompt name; records calls."""

    def __init__(self, replies: dict[str, dict]):
        self.replies = replies
        self.calls: list[tuple[str, dict]] = []
        self.in_tokens = self.out_tokens = 0
        self.cost_usd = 0.0

    def judge(self, prompt_name, payload, schema=None):
        self.calls.append((prompt_name, payload))
        return dict(self.replies.get(prompt_name, {"label": "unknown", "quote": "", "reason": ""}))


def vid(i: int, days_ago: float, secs: int = 1500, *, title: str | None = None, desc: str = "", captions=False,
        paid=False, platform="youtube", snap_id=None) -> ContentItem:
    return ContentItem(platform=platform, item_id=f"{platform[:2]}{i}", url=f"https://synth.test/{platform}/{i}",
                       title=title or f"Synthetic episode {i} about pricing your offer", published_at=NOW - timedelta(days=days_ago),
                       duration_s=secs, is_short=0 < secs <= 60, has_captions=captions, paid_placement=paid,
                       description=desc, snap_id=snap_id)


def make_creator(store: SnapshotStore, *, name="Synthetic Creator", description="Weekly lessons on pricing.",
                 youtube=True, video_count=40, subs=20000, country="US", items=None, pages=None,
                 feed=False, feed_desc="", site="https://synthcreator.test", links=None) -> CollectedCreator:
    """pages: {url: html_body}. Items get their own JSON snapshot so description quotes verify."""
    items = list(items or [])
    if items:
        body = json.dumps({"items": [{"id": it.item_id, "snippet": {"title": it.title, "description": it.description}}
                                     for it in items]})
        sid = store.save_snapshot("https://api.synth.test/videos", body.encode(), 200, "application/json", "synthetic")
        items = [it.model_copy(update={"snap_id": sid}) for it in items]
    c = CollectedCreator(candidate=Candidate(platform="youtube" if youtube else "podcast", source_id="UCsynth",
                                             url="https://www.youtube.com/channel/UCsynth", source="input_file"),
                         profile={"name": name, "description": description, "links": links or [], "site_links": []},
                         items=items)
    if youtube:
        chan = json.dumps({"items": [{"id": "UCsynth", "snippet": {"title": name, "description": description}}]})
        csid = store.save_snapshot("https://api.synth.test/channels", chan.encode(), 200, "application/json", "synthetic")
        c.profile["youtube"] = {"channel_id": "UCsynth", "title": name, "description": description, "country": country,
                                "url": "https://www.youtube.com/channel/UCsynth", "snap_id": csid,
                                "video_count": video_count, "subscribers": subs, "featured_channels": []}
        c.profile["followers"] = subs
        c.profile["country_declared"] = country
        c.snap_ids.append(csid)
    if feed:
        xml = f"<?xml version='1.0'?><rss><channel><title>{name}</title><description>{feed_desc}</description></channel></rss>"
        fsid = store.save_snapshot(site + "/feed.xml", xml.encode(), 200, "application/rss+xml", "synthetic")
        c.feed_meta = {"url": site + "/feed.xml", "snap_id": fsid, "description": feed_desc, "episode_count": len(items)}
    if pages:
        for url, body in pages.items():
            html = f"<html><body>{body}</body></html>"
            psid = store.save_snapshot(url, html.encode(), 200, "text/html", "synthetic")
            from receipts.util.text import visible_text
            from receipts.util.urls import extract_links

            c.website_pages[url] = (psid, visible_text(html))
            c.profile["site_links"] += extract_links(html, url)
        c.profile["website_url"] = site
    return c
