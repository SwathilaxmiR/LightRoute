"""Check c5: reachable. Contact routes come only from creator-owned sources (G3):
the creator's own website pages, their channel description, their own feed. Nothing is guessed."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from rapidfuzz import fuzz

from receipts.checks.base import CheckResult, failed, passed
from receipts.guards import assert_contact_provenance, assisted_flag
from receipts.models import CollectedCreator
from receipts.util.dates import within_days
from receipts.util.text import normalise, sentence_around
from receipts.util.urls import extract_emails, host_of, normalise_handle, registrable_domain

CONTACT_PATH = re.compile(r"/(contact|sponsor|sponsorship|advertise|guest|pitch|work-with|partner|press|booking|media-kit)",
                          re.I)
SOCIAL = {
    "linkedin": (re.compile(r"linkedin\.com/(in|company)/([A-Za-z0-9_.%-]+)", re.I), ("linkedin.com",)),
    "instagram": (re.compile(r"instagram\.com/([A-Za-z0-9_.]+)", re.I), ("instagram.com",)),
    "x": (re.compile(r"(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})(?:[/?]|$)", re.I), ("x.com", "twitter.com")),
}
SOCIAL_NON_HANDLES = {"p", "reel", "reels", "explore", "share", "sharer", "intent", "home", "i", "hashtag", "search",
                      "login", "accounts", "stories", "tv"}


@dataclass
class Route:
    channel_type: str
    handle_or_url: str
    source_url: str
    source_quote: str
    snap_id: int | None
    creator_owned: bool = True
    business_language: bool = False
    live_ok: bool = False
    name_match: bool = False
    mx_ok: bool | None = None
    last_active: str | None = None
    active_proof: str | None = None
    manual_check_path: str | None = None
    channel_score: float = 0.0
    role: str = "candidate"
    assisted_send: bool = False
    verified: bool = False
    notes: list[str] = field(default_factory=list)


def business_words(text: str, words: list[str]) -> bool:
    t = normalise(text)
    return any(w in t for w in words)


def owned_hosts(c: CollectedCreator) -> set[str]:
    hs = set()
    site = c.profile.get("website_url")
    if site:
        hs.add(host_of(site))
    return hs


def is_owned_page(url: str, c: CollectedCreator) -> bool:
    return host_of(url) in owned_hosts(c)


def name_tokens(c: CollectedCreator) -> list[str]:
    toks = [t for t in re.findall(r"[a-z0-9]{4,}", normalise(c.profile.get("name", ""))) if t not in
            {"show", "podcast", "channel", "with", "official", "the", "and"}]
    site = c.profile.get("website_url")
    if site:
        toks.append(registrable_domain(site).split(".")[0])
    return list(dict.fromkeys(toks))


def name_match_text(text: str, c: CollectedCreator) -> bool:
    t = normalise(text)
    return any(tok in t for tok in name_tokens(c))


def name_match_handle(handle: str, c: CollectedCreator, ratio: float) -> bool:
    slug = re.sub(r"[^a-z0-9]", "", handle.split("/")[-1].lower())
    name = re.sub(r"[^a-z0-9]", "", normalise(c.profile.get("name", "")))
    if not slug or not name:
        return False
    return fuzz.partial_ratio(slug, name) >= ratio or any(tok in slug for tok in name_tokens(c))


def extract_routes(c: CollectedCreator, store, s) -> list[Route]:
    """All contact routes found on creator-owned sources. Third-party pages are ignored."""
    words = s.channels.business_words
    routes: list[Route] = []
    from receipts.sources.website import has_form, page_html

    # website pages (owned host only)
    for url, (sid, _) in c.website_pages.items():
        if not is_owned_page(url, c):
            continue
        text = store.load_snapshot_text(sid)
        for email, quote in extract_emails(text):
            routes.append(Route("email", email, url, quote, sid))
        if CONTACT_PATH.search(urlsplit(url).path):
            html = page_html(store, sid)
            visible = c.website_pages[url][1]
            hit = re.search(r"(contact|sponsor|guest|work with|partner|press|booking|advertis|pitch|inquir|enquir)",
                            visible, re.I)
            quote = sentence_around(visible, hit.start(), hit.end()) if hit else visible[:160]
            if has_form(html):
                routes.append(Route("form", url, url, quote or url, sid))
            else:
                routes.append(Route("website", url, url, quote or url, sid))
        for link in re.findall(r"https?://[^\s\"'<>]+", text):
            routes += _social_route(link, url, sid)
    # channel description
    yt = c.profile.get("youtube")
    if yt and yt.get("description"):
        d = yt["description"]
        for email, quote in extract_emails(d):
            routes.append(Route("email", email, yt["url"], quote, yt.get("snap_id")))
        if business_words(d, words) and (m := re.search(r"(business|inquir|enquir|sponsor|collab|booking|contact)", d, re.I)):
            routes.append(Route("youtube", yt["url"] + "/about", yt["url"], sentence_around(d, m.start(), m.end()),
                                yt.get("snap_id")))
        for link in re.findall(r"https?://[^\s\"'<>]+", d):
            routes += _social_route(link, yt["url"], yt.get("snap_id"))
    # feed owner email: only if corroborated on an owned page, or business wording near it in the feed text
    fm = c.feed_meta or {}
    owner = (fm.get("owner_email") or "").lower()
    if owner:
        on_page = [r for r in routes if r.channel_type == "email" and r.handle_or_url.lower() == owner]
        if not on_page and fm.get("snap_id"):
            feed_text = store.load_snapshot_text(fm["snap_id"])
            i = feed_text.lower().find(owner)
            if i >= 0:
                quote = sentence_around(feed_text, i, i + len(owner))
                if business_words(quote, words):
                    routes.append(Route("email", owner, fm["url"], quote, fm["snap_id"]))
    # dedupe on (type, handle): keep the first (site pages come first)
    seen, out = set(), []
    for r in routes:
        k = (r.channel_type, r.handle_or_url.lower())
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


def _social_route(link: str, page_url: str, sid: int | None) -> list[Route]:
    for plat, (pat, hosts) in SOCIAL.items():
        h = host_of(link)
        if not any(h == x or h.endswith("." + x) for x in hosts):
            continue
        m = pat.search(link)
        if not m:
            continue
        handle = normalise_handle(plat, link)
        if handle.split("/")[-1] in SOCIAL_NON_HANDLES:
            continue
        return [Route(plat, link.split("?")[0].rstrip("/"), page_url, link, sid)]
    return []


def verify_routes(routes: list[Route], c: CollectedCreator, s, now, mx: dict[str, bool | None],
                  manual: dict[tuple[str, str], dict]) -> None:
    """Fill business_language, live_ok, name_match, mx_ok, activity, verified in place."""
    words = s.channels.business_words
    latest = max((i.published_at for i in c.items), default=None)
    latest_proof = "api" if any(i.platform == "youtube" for i in c.items) else "feed"
    for r in routes:
        assert_contact_provenance(r.handle_or_url, r.source_url, r.source_quote, r.channel_type)  # G3
        r.business_language = business_words(r.source_quote, words)
        r.assisted_send = assisted_flag(r.channel_type, s.channels.assisted_types)
        src_text = next((t for u, (_, t) in c.website_pages.items() if u == r.source_url), None)
        if src_text is None:
            yt = c.profile.get("youtube") or {}
            src_text = yt.get("description", "") if r.source_url == yt.get("url") else (c.feed_meta or {}).get("description", "")
        if r.channel_type in ("linkedin", "instagram", "x"):
            r.live_ok = True  # linked from a creator-owned page; never fetched (no logins, no scraping)
            r.name_match = name_match_handle(normalise_handle(r.channel_type, r.handle_or_url), c,
                                             s.identity.handle_name_match_ratio)
            mc = manual.get((r.channel_type, r.handle_or_url))
            if mc:
                r.manual_check_path = mc.get("manual_check_path")
                r.last_active = mc.get("last_active")
                r.active_proof = "manual"
            active = bool(r.last_active) and within_days(_d(r.last_active), s.channels.active_window_days, now)
            r.verified = bool(r.live_ok and r.name_match and active and r.manual_check_path)
            if not r.manual_check_path:
                r.notes.append("manual check required")
            continue
        r.live_ok = r.snap_id is not None  # the page/API response was fetched with 2xx and snapshotted
        r.name_match = name_match_text(src_text or "", c) or name_match_handle(r.handle_or_url, c,
                                                                                s.identity.handle_name_match_ratio)
        if r.channel_type == "email":
            r.mx_ok = mx.get(r.handle_or_url.split("@")[-1].lower())
        if latest is not None:
            r.last_active = latest.date().isoformat()
            r.active_proof = latest_proof if r.channel_type in ("youtube", "email", "form", "website") else None
        active = latest is not None and within_days(latest, s.channels.active_window_days, now)
        r.verified = bool(r.live_ok and r.name_match and active and (r.channel_type != "email" or r.mx_ok))


def _d(iso: str):
    from receipts.util.dates import parse_dt

    return parse_dt(iso)


def score_routes(routes: list[Route], c: CollectedCreator, s, responsiveness: float) -> None:
    w = s.channels
    for r in routes:
        owned = is_owned_page(r.source_url, c) or r.source_url == (c.profile.get("youtube") or {}).get("url")
        r.channel_score = (w.weight_business_language * r.business_language + w.weight_creator_owned * owned
                           + w.weight_automatable * (r.channel_type in ("email", "form")) + responsiveness)


def responsiveness_points(reply_rate: float | None, has_guest_or_sponsor_form: bool, s) -> float:
    w = s.channels
    pts = 0.0
    if reply_rate is not None:
        pts += 2 if reply_rate >= w.reply_rate_plus2 else 1 if reply_rate >= w.reply_rate_plus1 else 0
    pts += 1 if has_guest_or_sponsor_form else 0
    return min(pts, w.responsiveness_cap)


def select(routes: list[Route]) -> tuple[Route | None, Route | None]:
    """best = highest-scoring verified route with business wording; backup = best of another channel type."""
    ver = sorted([r for r in routes if r.verified], key=lambda r: (-r.channel_score, r.channel_type))
    best = next((r for r in ver if r.business_language), None)
    if best is None:
        return None, None
    backup = next((r for r in ver if r.channel_type != best.channel_type), None)
    if backup is None:
        backup = next((r for r in ver if r is not best and r.channel_type in ("youtube", "website")), None)
    best.role = "best"
    if backup:
        backup.role = "backup"
    return best, backup


def reachable_result(best: Route | None, backup: Route | None, routes: list[Route]) -> CheckResult:
    if best is None:
        reason = "no_business_contact" if any(r.verified for r in routes) else "channel_unverified"
        return failed("reachable", reason, quote_or_metric=f"routes={len(routes)}; verified={sum(r.verified for r in routes)}")
    if backup is None:
        return failed("reachable", "channel_unverified", evidence_url=best.source_url, snap_id=best.snap_id,
                      quote_or_metric=f"best={best.channel_type}; backup=none")
    q = best.source_quote
    return passed("reachable", evidence_url=best.source_url, snap_id=best.snap_id, quote_or_metric=q,
                  details={"best": best.channel_type, "backup": backup.channel_type, "handle": best.handle_or_url,
                           "mx_ok": best.mx_ok})
