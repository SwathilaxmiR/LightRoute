"""Check c1: real and relevant = not an agency/brand/publication/fan account, and one of the 3 segments.

Deterministic first; the LLM (judge_segment) is consulted only for ambiguous cases and its
quote must verify against the snapshot it came from.
"""
from __future__ import annotations

import re
import statistics

from receipts.checks.base import CheckResult, failed, metric, passed
from receipts.checks.c2_active import compute_activity, is_longform
from receipts.checks.offers import Offer
from receipts.llm.client import accept_llm_claim
from receipts.models import CollectedCreator, Segment
from receipts.util.dates import within_days
from receipts.util.text import normalise
from receipts.util.urls import registrable_domain

CREATOR_LABELS = {"podcaster", "youtuber", "expert_led"}
NON_CREATOR_REASON = {"agency_or_brand": "agency_or_brand", "publication_or_directory": "publication_or_directory",
                      "fan_account": "fan_account"}


def _kw_hit(text: str, keywords: list[str]) -> str | None:
    t = normalise(text)
    for kw in keywords:
        if re.search(r"(?<![a-z0-9])" + re.escape(normalise(kw)) + r"(?![a-z0-9])", t):
            return kw
    return None


def _llm_segment(c: CollectedCreator, llm, store, min_chars: int) -> tuple[dict | None, int | None, str]:
    """Ask judge_segment on the profile description; return (claim, snap_id, url) if quote verifies."""
    if llm is None:
        return None, None, ""
    yt = c.profile.get("youtube") or {}
    sid = yt.get("snap_id") or (c.feed_meta or {}).get("snap_id")
    url = yt.get("url") or (c.feed_meta or {}).get("url", "")
    if not sid and c.website_pages:
        url, (sid, _) = next(iter(c.website_pages.items()))
    text = f"Name: {c.profile.get('name', '')}\nDescription: {c.profile.get('description', '')}"
    claim = llm.judge("judge_segment", {"text": text})
    ok = claim.get("quote") and accept_llm_claim(claim, store, sid, min_chars)
    claim["_verified"] = bool(ok)
    return claim, sid, url


def check_exclusions(c: CollectedCreator, s, llm=None, store=None) -> CheckResult | None:
    """None if clean; a failing CheckResult otherwise."""
    ex = s.exclusions
    kws = ex.get("name_or_description_keywords", [])
    name = c.profile.get("name", "")
    desc = c.profile.get("description", "")
    ev = (c.profile.get("youtube") or {}).get("url") or c.candidate.url
    if (kw := _kw_hit(name, kws)):
        return failed("real_relevant", "excluded_keyword", evidence_url=ev, quote_or_metric=f"name contains '{kw}': {name}")
    site = c.profile.get("website_url")
    if c.candidate.platform == "website" and site and registrable_domain(site) in ex.get("domains_blocklist", []):
        return failed("real_relevant", "blocklisted_domain", evidence_url=site, quote_or_metric=registrable_domain(site))
    f = c.profile.get("followers")
    if f is not None and f > s.thresholds.max_followers_hard:
        return failed("real_relevant", "agency_or_brand", evidence_url=ev,
                      quote_or_metric=metric(followers=f, max_followers_hard=s.thresholds.max_followers_hard))
    if (kw := _kw_hit(desc, kws)):
        claim, sid, url = _llm_segment(c, llm, store, s.text.quote_min_chars)
        if claim and claim["_verified"] and claim["label"] in CREATOR_LABELS:
            return None  # ambiguous keyword, LLM says creator with a verified quote
        if claim and claim["_verified"] and claim["label"] in NON_CREATOR_REASON:
            return failed("real_relevant", NON_CREATOR_REASON[claim["label"]], evidence_url=url, snap_id=sid,
                          quote_or_metric=claim["quote"], verified_by="llm_verified")
        return failed("real_relevant", "excluded_keyword", evidence_url=ev,
                      quote_or_metric=f"description contains '{kw}' (ambiguous; no verified LLM judgement)")
    return None


def decide_segment(c: CollectedCreator, s, offers: list[Offer], now, llm=None, store=None
                   ) -> tuple[Segment | None, CheckResult]:
    thr = s.thresholds
    pod = [i for i in c.items if i.platform == "podcast"]
    yt_items = [i for i in c.items if i.platform == "youtube"]
    fm = c.feed_meta or {}

    # (a) podcaster
    durs = [i.duration_s for i in pod if i.duration_s]
    if c.feed_meta is not None and len(pod) >= thr.podcast_min_episodes and durs:
        med_min = statistics.median(durs) / 60
        if med_min >= thr.podcast_min_median_minutes:
            m = metric(episodes=len(pod), median_minutes=round(med_min, 1), feed=fm.get("url", ""))
            return Segment.podcaster, passed("real_relevant", evidence_url=fm.get("url"), quote_or_metric=m,
                                             snap_id=fm.get("snap_id"), details={"segment": "podcaster"})

    service = [o for o in offers if o.klass == "service"]
    yt = c.profile.get("youtube")
    # (b) youtuber
    long60 = [i for i in yt_items if is_longform(i.duration_s, thr) and within_days(i.published_at, thr.window_days_long, now)]
    if yt and len(long60) >= thr.active_longform_60d and not service:
        m = metric(longform_60d=len(long60), coaching_or_service_offer="none", items=[i.url for i in long60[:5]])
        return Segment.youtuber, passed("real_relevant", evidence_url=yt["url"], quote_or_metric=m,
                                        snap_id=yt.get("snap_id"), details={"segment": "youtuber"})

    # (c) expert_led: an offer plus regular publishing
    sellable = [o for o in offers if o.klass in ("service", "course")]
    if sellable and compute_activity(c.items, now, thr)["items_60d"] >= s.segment.expert_min_items_60d:
        o = sellable[0]
        return Segment.expert_led, passed("real_relevant", evidence_url=o.url, quote_or_metric=o.quote,
                                          snap_id=o.snap_id, details={"segment": "expert_led", "offer": o.klass})

    # (d) LLM
    claim, sid, url = _llm_segment(c, llm, store, s.text.quote_min_chars)
    if claim and claim["_verified"] and claim["label"] in CREATOR_LABELS:
        return Segment(claim["label"]), passed("real_relevant", evidence_url=url, quote_or_metric=claim["quote"],
                                               snap_id=sid, verified_by="llm_verified",
                                               details={"segment": claim["label"]})
    reason = "unverified_llm_claim" if claim and claim.get("quote") and not claim["_verified"] else "not_creator_segment"
    return None, failed("real_relevant", "not_creator_segment", evidence_url=c.candidate.url,
                        quote_or_metric=metric(podcast_items=len(pod), youtube_longform_60d=len(long60),
                                               offers=len(sellable), llm=reason),
                        details={"llm_claim": claim})
