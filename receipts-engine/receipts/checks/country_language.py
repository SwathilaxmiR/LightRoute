"""Country (declared + corroborated) and language checks."""
from __future__ import annotations

import re
from dataclasses import dataclass

from receipts.checks.base import CheckResult, failed, metric, passed
from receipts.models import ContentItem
from receipts.util.text import english_share
from receipts.util.urls import registrable_domain


@dataclass
class Signal:
    country: str
    kind: str        # cctld | name | postal | schema
    value: str
    url: str
    snap_id: int | None = None


def to_iso2(value: str | None, cfg: dict) -> str | None:
    if not value:
        return None
    v = value.strip()
    if len(v) == 2:
        v2 = v.upper()
        return "GB" if v2 == "UK" else v2
    low = v.casefold()
    for iso, names in cfg.get("names", {}).items():
        if low in [n.casefold() for n in names]:
            return iso
    return None


def cctld_signal(website_url: str | None, cfg: dict) -> Signal | None:
    if not website_url:
        return None
    reg = registrable_domain(website_url)
    for iso, sufs in cfg.get("cctld", {}).items():
        if any(reg.endswith(s) for s in sufs):
            return Signal(iso, "cctld", reg, website_url)
    last = reg.rsplit(".", 1)[-1]
    if len(last) == 2 and last not in cfg.get("generic_cctlds", []):
        return Signal(last.upper(), "cctld", reg, website_url)
    return None


def text_signals(texts: list[tuple[str, int | None, str]], cfg: dict) -> list[Signal]:
    out: list[Signal] = []
    for url, sid, text in texts:
        low = text.casefold()
        for iso, names in cfg.get("names", {}).items():
            for n in names:
                if re.search(r"(?<![a-z0-9])" + re.escape(n.casefold()) + r"(?![a-z0-9])", low):
                    out.append(Signal(iso, "name", n, url, sid))
                    break
        for iso, pat in cfg.get("postal_patterns", {}).items():
            m = re.search(pat, text)
            if m:
                out.append(Signal(iso, "postal", m.group(0), url, sid))
    return out


def evaluate_country(declared: str | None, declared_src: tuple[str, str, int | None] | None,
                     website_url: str | None, texts: list[tuple[str, int | None, str]],
                     schema_countries: list[tuple[str, int | None, str]], cfg: dict) -> CheckResult:
    """declared_src = (label, url, snap_id). texts/schema_countries = [(url, snap_id, value)]."""
    approved = cfg.get("approved", [])
    decl = to_iso2(declared, cfg)
    sigs: list[Signal] = []
    if decl is None and schema_countries:
        url, sid, val = schema_countries[0]
        decl = to_iso2(val, cfg)
        declared_src = ("schema.org addressCountry", url, sid)
        schema_countries = schema_countries[1:]
    for url, sid, val in schema_countries:
        iso = to_iso2(val, cfg)
        if iso:
            sigs.append(Signal(iso, "schema", val, url, sid))
    cc = cctld_signal(website_url, cfg)
    if cc:
        sigs.append(cc)
    sigs += text_signals(texts, cfg)

    if decl is None:
        return failed("country", "country_unverified", quote_or_metric="declared=none",
                      details={"signals": [s.__dict__ for s in sigs]})
    agree = [s for s in sigs if s.country == decl]
    src_label, src_url, src_sid = declared_src or ("unknown", "", None)
    corr = "; ".join(f"{s.kind}:{s.value!r} ({s.url})" for s in agree[:3]) or "none"
    m = f"declared={decl} ({src_label}, {src_url}); corroborated_by={corr}"
    details = {"declared": decl, "signals": [s.__dict__ for s in sigs]}
    if not agree:
        return failed("country", "country_unverified", evidence_url=src_url, quote_or_metric=m, snap_id=src_sid,
                      details=details)
    if decl not in approved:
        return failed("country", "country_not_approved", evidence_url=src_url, quote_or_metric=m, snap_id=src_sid,
                      details=details)
    return passed("country", evidence_url=agree[0].url or src_url, quote_or_metric=m, snap_id=agree[0].snap_id or src_sid,
                  details=details)


def evaluate_language(items: list[ContentItem], thr, text_cfg=None) -> CheckResult:
    recent = sorted(items, key=lambda i: i.published_at, reverse=True)[: thr.language_items]
    texts = [f"{i.title or ''}. {(i.description or '')[:300]}" for i in recent]
    kw = {}
    if text_cfg is not None:
        kw = {"min_confidence": text_cfg.lang_min_confidence, "min_letters": text_cfg.lang_min_letters}
    en, total = english_share(texts, **kw)
    need = min(thr.language_min_english, total)
    m = metric(english_items=en, checked=total, required=thr.language_min_english, items=[i.url for i in recent])
    ev = recent[0].url if recent else None
    if total == 0 or en < need:
        return failed("language", "not_english", evidence_url=ev, quote_or_metric=m)
    return passed("language", evidence_url=ev, quote_or_metric=m)
