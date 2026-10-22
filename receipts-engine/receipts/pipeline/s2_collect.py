"""s2: collect profile, content items and creator-owned website pages for one candidate.

Cross-platform links are followed only from creator-owned sources (channel description,
the creator's own site): a podcast's YouTube channel is found through links on its site,
never by name search.
"""
from __future__ import annotations

import re

from receipts.guards import RobotsDisallowed
from receipts.http import FixtureMissing
from receipts.models import Candidate, CollectedCreator, ContentItem
from receipts.pipeline.common import Reject
from receipts.sources.rss import parse_feed
from receipts.sources.website import fetch_site, page_links
from receipts.sources.youtube import YTChannel
from receipts.util.urls import (
    canonical_url,
    host_of,
    is_shared_host,
    registrable_domain,
    youtube_channel_url,
)

URL_IN_TEXT = re.compile(r"https?://[^\s<>\"')\]]+", re.I)
YT_CHANNEL = re.compile(r"youtube\.com/channel/(UC[0-9A-Za-z_-]{22})", re.I)
YT_HANDLE = re.compile(r"youtube\.com/@([A-Za-z0-9._-]+)", re.I)
FEED_LINK = re.compile(r"(\.xml$|/feed/?$|/rss/?$|feeds\.|/podcast\.rss)", re.I)


def urls_in_text(text: str) -> list[str]:
    return [u.rstrip(".,;:!?") for u in URL_IN_TEXT.findall(text or "")]


def monetization_domains(s) -> set[str]:
    m = s.monetization
    return {d.lower() for k in ("course_or_product", "membership_or_subscription", "services_or_booking")
            for d in m.get(k, [])}


def pick_website(links: list[str], s, name_hint: str = "") -> str | None:
    """First creator-owned-looking link: not a shared host, not blocklisted, not a monetization platform.
    Prefer one whose domain contains a token of the creator's name."""
    shared = s.exclusions.get("shared_hosts", [])
    block = set(s.exclusions.get("domains_blocklist", []))
    money = monetization_domains(s)
    ok = []
    for u in links:
        reg = registrable_domain(u)
        if not reg or reg in block or reg in money or is_shared_host(u, shared):
            continue
        ok.append(u)
    if not ok:
        return None
    tokens = [t for t in re.findall(r"[a-z0-9]{4,}", name_hint.lower())]
    for u in ok:
        if any(t in registrable_domain(u) for t in tokens):
            return _root(u)
    return _root(ok[0])


def _root(u: str) -> str:
    c = canonical_url(u)
    m = re.match(r"(https?://[^/]+)", c)
    return m.group(1) if m else c


async def _youtube(ctx, ref: str) -> tuple[YTChannel, list[ContentItem]]:
    if ref.startswith("@"):
        ch = await ctx.yt.resolve_handle(ref)
        if ch is None:
            raise Reject("unresolvable_handle", ref, "collected")
    else:
        chs = await ctx.yt.channels([ref])
        if not chs:
            raise Reject("channel_not_found", ref, "collected")
        ch = chs[0]
    items: list[ContentItem] = []
    if ch.uploads_playlist:
        ups = await ctx.yt.uploads(ch.uploads_playlist, max_items=50)
        if ups:
            items = await ctx.yt.videos([u.video_id for u in ups])
    return ch, items


async def _site(ctx, url: str | None) -> dict[str, tuple[int, str]]:
    if not url:
        return {}
    try:
        return await fetch_site(url, ctx.http)
    except (RobotsDisallowed, FixtureMissing):
        return {}


def _site_links(ctx, pages: dict[str, tuple[int, str]]) -> list[str]:
    out: list[str] = []
    for url, (sid, _) in pages.items():
        out += page_links(ctx.store, sid, url)
    return list(dict.fromkeys(out))


def _yt_ref_from_links(links: list[str]) -> str | None:
    for u in links:
        if m := YT_CHANNEL.search(u):
            return m.group(1)
    for u in links:
        if m := YT_HANDLE.search(u):
            return "@" + m.group(1).lower()
    return None


def _feed_from_links(links: list[str], site: str | None) -> str | None:
    if not site:
        return None
    for u in links:
        if FEED_LINK.search(u.split("?")[0]) and (host_of(u) == host_of(site) or "feeds." in host_of(u)):
            return u
    return None


def _apply_youtube(c: CollectedCreator, ch: YTChannel, items: list[ContentItem]) -> None:
    c.items += items
    c.snap_ids += [ch.snap_id] + [i.snap_id for i in items]
    c.profile.setdefault("name", ch.title)
    c.profile.setdefault("description", ch.description)
    c.profile["youtube"] = {
        "channel_id": ch.channel_id, "title": ch.title, "description": ch.description, "country": ch.country,
        "custom_url": ch.custom_url, "subscribers": ch.subscribers, "hidden_subscribers": ch.hidden_subscribers,
        "video_count": ch.video_count, "url": youtube_channel_url(ch.channel_id), "snap_id": ch.snap_id,
        "featured_channels": ch.featured_channels,
    }
    if ch.country and not c.profile.get("country_declared"):
        c.profile["country_declared"] = ch.country
        c.profile["country_declared_source"] = ("youtube snippet.country", youtube_channel_url(ch.channel_id), ch.snap_id)
    if c.profile.get("followers") is None and ch.subscribers is not None:
        c.profile["followers"] = ch.subscribers
    c.profile["links"] = list(dict.fromkeys(c.profile.get("links", []) + urls_in_text(ch.description)))


async def _apply_feed(ctx, c: CollectedCreator, feed_url: str, pi_feed=None) -> bool:
    try:
        parsed = await parse_feed(ctx.http, feed_url)
    except (RobotsDisallowed, FixtureMissing):
        parsed = None
    items: list[ContentItem] = []
    meta = None
    if parsed:
        meta, items = parsed
    elif pi_feed is not None:
        items = await ctx.pi.episodes_by_feed_id(pi_feed.feed_id)
    if not items and meta is None:
        return False
    c.items += items
    c.snap_ids += [i.snap_id for i in items] + ([meta.snap_id] if meta else [])
    fm = meta.model_dump() if meta else {"url": feed_url}
    if pi_feed is not None:
        fm.update({"feed_id": pi_feed.feed_id, "pi_snap_id": pi_feed.snap_id, "itunes_id": pi_feed.itunes_id,
                   "pi_episode_count": pi_feed.episode_count})
        c.snap_ids.append(pi_feed.snap_id)
    c.feed_meta = fm
    c.profile.setdefault("name", (meta.title if meta else "") or (pi_feed.title if pi_feed else ""))
    c.profile.setdefault("description", (meta.description if meta else "") or (pi_feed.description if pi_feed else ""))
    c.profile.setdefault("language_declared", (meta.language if meta else "") or (pi_feed.language if pi_feed else ""))
    return True


async def collect(ctx, cand: Candidate) -> CollectedCreator:
    s = ctx.settings
    c = CollectedCreator(candidate=cand, profile={"links": []})
    site: str | None = None

    if cand.platform == "youtube":
        ch, items = await _youtube(ctx, cand.source_id)
        _apply_youtube(c, ch, items)
        site = pick_website(c.profile["links"], s, ch.title)
    elif cand.platform == "podcast":
        pi_feed = None
        if cand.source_id.isdigit():
            pi_feed = await ctx.pi.by_feed_id(cand.source_id)
            if pi_feed is None:
                raise Reject("feed_not_found", cand.source_id, "collected")
            feed_url = pi_feed.url
            if pi_feed.link:
                site = pick_website([pi_feed.link], s, pi_feed.title)
        else:
            feed_url = cand.hint.get("feed_url") or cand.url
        if not await _apply_feed(ctx, c, feed_url, pi_feed):
            raise Reject("feed_unreadable", feed_url, "collected")
        if not site and c.feed_meta and c.feed_meta.get("link"):
            site = pick_website([c.feed_meta["link"]], s, c.profile.get("name", ""))
    else:
        if registrable_domain(cand.url) in s.exclusions.get("domains_blocklist", []):
            raise Reject("blocklisted_domain", cand.url, "collected")
        site = cand.url if is_shared_host(cand.url, s.exclusions.get("shared_hosts", [])) else _root(cand.url)

    pages = await _site(ctx, site)
    if cand.platform == "website" and not pages:
        raise Reject("site_unreachable", cand.url, "collected")
    if pages:
        c.website_pages = pages
        c.snap_ids += [sid for sid, _ in pages.values()]
        c.profile["website_url"] = site
        site_links = _site_links(ctx, pages)
        c.profile["site_links"] = site_links
        # secondary platforms, only via the creator's own pages
        if "youtube" not in c.profile:
            ref = _yt_ref_from_links(site_links)
            if ref:
                try:
                    ch, items = await _youtube(ctx, ref)
                    _apply_youtube(c, ch, items)
                except (Reject, FixtureMissing):
                    pass
        if c.feed_meta is None:
            feed = _feed_from_links(site_links, site)
            if feed:
                await _apply_feed(ctx, c, feed)
        if cand.platform == "website":
            home_sid = pages[site][0] if site in pages else next(iter(pages.values()))[0]
            raw = ctx.store.load_raw(home_sid).decode("utf-8", errors="replace")
            m = re.search(r"<title[^>]*>(.*?)</title>", raw, re.I | re.S)
            c.profile.setdefault("name", (m.group(1).strip() if m else registrable_domain(site)).split("|")[0].strip())
            c.profile.setdefault("description", next(iter(pages.values()))[1][:500])

    c.profile.setdefault("name", cand.source_id)
    c.profile.setdefault("description", "")
    c.snap_ids = [i for i in dict.fromkeys(c.snap_ids) if i]
    return c
