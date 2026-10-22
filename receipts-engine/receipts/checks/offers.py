"""Offer / monetization evidence found in creator-owned text and links (rules, no LLM)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from receipts.checks.sources_text import TextSource, creator_texts
from receipts.models import CollectedCreator
from receipts.util.text import sentence_around
from receipts.util.urls import registrable_domain

SERVICE_WORDS = re.compile(
    r"\b(1:1 coaching|1-on-1 coaching|one[- ]on[- ]one coaching|coaching (?:program|programme|calls?|sessions?|packages?)"
    r"|book a (?:free )?(?:call|session|consultation|discovery call)|consulting services|consultancy|work with me"
    r"|hire me|strategy call|private coaching|mentorship program)\b", re.I)
COURSE_WORDS = re.compile(
    r"\b(online course|my course|our course|masterclass|cohort|enrol(?:l)? now|join the (?:course|program|programme)"
    r"|academy|bootcamp)\b", re.I)
MEMBERSHIP_WORDS = re.compile(r"\b(join the membership|patreon|supporters? club|become a (?:member|supporter)"
                              r"|paid subscribers?|premium feed)\b", re.I)
AFFILIATE_WORDS = re.compile(r"\b(use code|promo code|affiliate link|affiliate disclosure|i earn a commission"
                             r"|commission at no extra cost)\b", re.I)
COURSE_PATH = re.compile(r"/(courses?|shop|store|programs?|programmes?)(/|$)", re.I)


@dataclass
class Offer:
    klass: str            # service | course | product | membership | affiliate | sponsorship
    url: str
    quote: str
    snap_id: int | None
    via: str              # link | text | path


def _link_offers(c: CollectedCreator, src: TextSource, links: list[str], m: dict) -> list[Offer]:
    out = []
    for u in links:
        reg = registrable_domain(u)
        if reg in m.get("services_or_booking", []):
            out.append(Offer("service", src.url, u, src.snap_id, "link"))
        elif reg in m.get("course_or_product", []):
            out.append(Offer("course", src.url, u, src.snap_id, "link"))
        elif reg in m.get("membership_or_subscription", []):
            out.append(Offer("membership", src.url, u, src.snap_id, "link"))
    return out


def find_offers(c: CollectedCreator, s, store=None) -> list[Offer]:
    m = s.monetization
    out: list[Offer] = []
    for src in creator_texts(c, include_items=False):
        for klass, pat in (("service", SERVICE_WORDS), ("course", COURSE_WORDS), ("membership", MEMBERSHIP_WORDS)):
            hit = pat.search(src.text)
            if hit:
                out.append(Offer(klass, src.url, sentence_around(src.text, hit.start(), hit.end()), src.snap_id, "text"))
        links = re.findall(r"https?://[^\s<>\"')\]]+", src.text)
        out += _link_offers(c, src, links, m)
    # hrefs on the creator's own pages
    if store is not None:
        from receipts.sources.website import page_links

        for url, (sid, _) in c.website_pages.items():
            src = TextSource(url, sid, "", "site")
            hrefs = page_links(store, sid, url)
            out += _link_offers(c, src, hrefs, m)
        for url, (sid, text) in c.website_pages.items():
            if COURSE_PATH.search(urlsplit(url).path) and len(text.strip()) >= 12:
                out.append(Offer("course", url, sentence_around(text, 0, min(len(text), 80), 160), sid, "path"))
    # dedupe by (class, url, quote)
    seen, uniq = set(), []
    for o in out:
        k = (o.klass, o.url, o.quote)
        if k not in seen:
            seen.add(k)
            uniq.append(o)
    return uniq
