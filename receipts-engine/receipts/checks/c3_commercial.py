"""Check c3: commercial. Pass if at least one verified monetization class:
sponsorship | product_or_course | membership | affiliate | service. One receipt per class (sub_key).
Rules first; ambiguous pages go to judge_commercial and are accepted only with a verified quote."""
from __future__ import annotations

import re

from receipts.checks.base import CheckResult, failed, metric, passed
from receipts.checks.offers import Offer
from receipts.checks.sources_text import creator_texts
from receipts.llm.client import accept_llm_claim
from receipts.models import CollectedCreator
from receipts.util.dates import within_days
from receipts.util.text import sentence_around, verify_quote

CLASSES = ["sponsorship", "product_or_course", "membership", "affiliate", "service"]
OFFER_TO_CLASS = {"course": "product_or_course", "product": "product_or_course", "membership": "membership",
                  "service": "service"}
LLM_TO_CLASS = {"sponsorship": "sponsorship", "product": "product_or_course", "course": "product_or_course",
                "membership": "membership", "subscription": "membership", "affiliate": "affiliate",
                "service": "service"}
SPONSOR_PAGE = re.compile(r"/(sponsor|sponsorship|advertise|media-kit|partner|partnerships|work-with)", re.I)


def _phrase_hit(text: str, phrases: list[str]) -> tuple[int, int] | None:
    """(start, end) of the first phrase found, case-insensitive."""
    low = text.casefold()
    for p in phrases:
        i = low.find(p.casefold())
        if i >= 0:
            return i, i + len(p)
    return None


def _verified(store, snap_id, quote, min_chars) -> bool:
    return bool(snap_id) and verify_quote(quote, store.load_snapshot_text(snap_id), min_chars)


def commercial_evidence(c: CollectedCreator, s, offers: list[Offer], now, store, llm=None) -> list[CheckResult]:
    """Return one passing CheckResult per verified class (strongest evidence first per class)."""
    m = s.monetization
    minc = s.text.quote_min_chars
    found: dict[str, CheckResult] = {}
    aff = [p.casefold() for p in m.get("affiliate_phrases", [])]
    sponsor_phrases = [p for p in m.get("sponsor_phrases", []) if p.casefold() not in aff]
    window = s.evidence.sponsorship_window_days

    recent = sorted([i for i in c.items if within_days(i.published_at, window, now)],
                    key=lambda i: i.published_at, reverse=True)
    # sponsorship: paid placement flag from the API
    for it in recent:
        if it.paid_placement:
            found["sponsorship"] = passed(
                "commercial", "sponsorship", evidence_url=it.url, snap_id=it.snap_id,
                quote_or_metric=metric(paidProductPlacement="true", item=it.url, published=it.published_at.date()),
                details={"phrase": "paid placement flag"})
            break
    # sponsorship / affiliate phrases in recent item descriptions
    for it in recent:
        d = it.description or ""
        if "sponsorship" not in found and (h := _phrase_hit(d, sponsor_phrases)):
            q = sentence_around(d, *h)
            if _verified(store, it.snap_id, q, minc):
                found["sponsorship"] = passed("commercial", "sponsorship", evidence_url=it.url, snap_id=it.snap_id,
                                              quote_or_metric=q, details={"phrase": d[h[0]:h[1]]})
        if "affiliate" not in found and (h := _phrase_hit(d, aff)):
            q = sentence_around(d, *h)
            if _verified(store, it.snap_id, q, minc):
                found["affiliate"] = passed("commercial", "affiliate", evidence_url=it.url, snap_id=it.snap_id,
                                            quote_or_metric=q, details={"phrase": d[h[0]:h[1]]})
    # affiliate on creator-owned pages too
    if "affiliate" not in found:
        for src in creator_texts(c, include_items=False):
            if (h := _phrase_hit(src.text, aff)):
                q = sentence_around(src.text, *h)
                if _verified(store, src.snap_id, q, minc):
                    found["affiliate"] = passed("commercial", "affiliate", evidence_url=src.url, snap_id=src.snap_id,
                                                quote_or_metric=q)
                    break
    # offers: course / product / membership / service
    for o in offers:
        cls = OFFER_TO_CLASS.get(o.klass)
        if cls and cls not in found and _verified(store, o.snap_id, o.quote, minc):
            found[cls] = passed("commercial", cls, evidence_url=o.url, snap_id=o.snap_id, quote_or_metric=o.quote,
                                details={"via": o.via})
    # sponsorship: a sponsor / advertise / media-kit page on the creator's own site
    if "sponsorship" not in found:
        for url, (sid, text) in c.website_pages.items():
            if SPONSOR_PAGE.search(url) and (h := _phrase_hit(text, m.get("sponsor_page_phrases", []))):
                q = sentence_around(text, *h)
                if _verified(store, sid, q, minc):
                    found["sponsorship"] = passed("commercial", "sponsorship", evidence_url=url, snap_id=sid,
                                                  quote_or_metric=q, details={"via": "sponsor_page"})
                    break
    # ambiguous: no rule found anything -> one LLM judgement on the most commercial-looking page
    if not found and llm is not None and c.website_pages:
        url, (sid, text) = sorted(c.website_pages.items(), key=lambda kv: not SPONSOR_PAGE.search(kv[0]))[0]
        claim = llm.judge("judge_commercial", {"text": text[:6000]})
        cls = LLM_TO_CLASS.get(claim.get("label", ""))
        if cls and accept_llm_claim(claim, store, sid, minc):
            found[cls] = passed("commercial", cls, evidence_url=url, snap_id=sid, quote_or_metric=claim["quote"],
                                verified_by="llm_verified")
        elif cls and claim.get("quote"):
            found["_unverified"] = failed("commercial", "unverified_llm_claim", "llm_unverified", evidence_url=url,
                                          snap_id=sid, quote_or_metric=claim["quote"], verified_by="llm_verified")
    return [found[k] for k in CLASSES if k in found] + ([found["_unverified"]] if "_unverified" in found else [])


def check_commercial(c: CollectedCreator, s, offers: list[Offer], now, store, llm=None
                     ) -> tuple[CheckResult, list[CheckResult]]:
    per = commercial_evidence(c, s, offers, now, store, llm)
    ok = [r for r in per if r.passed]
    if not ok:
        return failed("commercial", "no_commercial_signal", quote_or_metric="classes=none"), per
    first = ok[0]
    agg = passed("commercial", evidence_url=first.evidence_url, snap_id=first.snap_id,
                 quote_or_metric=metric(classes=[r.sub_key for r in ok]), details={"classes": [r.sub_key for r in ok]})
    return agg, per
