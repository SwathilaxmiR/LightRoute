"""s3: deterministic filters. Order: exclusions -> segment -> country -> language -> activity.
Any failure ends the candidate with a reason code. Every evaluated check is stored as a receipt."""
from __future__ import annotations

from dataclasses import dataclass, field

from receipts.checks.c1_real_relevant import check_exclusions, decide_segment
from receipts.checks.c2_active import check_active
from receipts.checks.country_language import evaluate_country, evaluate_language
from receipts.checks.offers import Offer, find_offers
from receipts.checks.sources_text import creator_texts
from receipts.models import CheckResult, CollectedCreator
from receipts.sources.website import page_html, parse_schema_org


@dataclass
class FilterResult:
    ok: bool
    segment: str | None = None
    country: str | None = None
    reason: str | None = None
    receipts: list[CheckResult] = field(default_factory=list)
    offers: list[Offer] = field(default_factory=list)
    activity: dict = field(default_factory=dict)


def country_inputs(c: CollectedCreator, store) -> tuple:
    declared = c.profile.get("country_declared")
    src = c.profile.get("country_declared_source")
    texts = [(t.url, t.snap_id, t.text) for t in creator_texts(c, include_items=False)]
    schema: list[tuple[str, int | None, str]] = []
    for url, (sid, _) in c.website_pages.items():
        for val in parse_schema_org(page_html(store, sid))["addressCountry"]:
            if isinstance(val, str):
                schema.append((url, sid, val))
    return declared, tuple(src) if src else None, c.profile.get("website_url"), texts, schema


def run_filters(ctx, c: CollectedCreator) -> FilterResult:
    s = ctx.settings
    out = FilterResult(ok=False)

    ex = check_exclusions(c, s, ctx.llm, ctx.store)
    if ex is not None:
        out.receipts.append(ex)
        out.reason = ex.reason_code
        return out

    out.offers = find_offers(c, s, ctx.store)
    seg, seg_r = decide_segment(c, s, out.offers, ctx.now, ctx.llm, ctx.store)
    out.receipts.append(seg_r)
    if seg is None:
        out.reason = seg_r.reason_code
        return out
    out.segment = seg.value
    out.receipts.append(CheckResult(check_name="segment", result="pass", evidence_url=seg_r.evidence_url,
                                    quote_or_metric=seg.value, snap_id=seg_r.snap_id, verified_by=seg_r.verified_by))

    country = evaluate_country(*country_inputs(c, ctx.store), s.countries)
    out.receipts.append(country)
    if not country.passed:
        out.reason = country.reason_code
        return out
    out.country = country.details.get("declared")

    lang = evaluate_language(c.items, s.thresholds, s.text)
    out.receipts.append(lang)
    if not lang.passed:
        out.reason = lang.reason_code
        return out

    act = check_active(c.items, ctx.now, s.thresholds)
    out.receipts.append(act)
    out.activity = act.details
    if not act.passed:
        out.reason = act.reason_code
        return out
    out.ok = True
    return out
