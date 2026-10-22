"""Check c2: active. Pass if count_30d >= active_count_30d or longform_60d >= active_longform_60d."""
from __future__ import annotations

from datetime import datetime

from receipts.checks.base import CheckResult, failed, metric, passed
from receipts.models import ContentItem
from receipts.util.dates import within_days, ymd
from receipts.util.text import normalise


def is_short(duration_s: int | None, thr) -> bool:
    return duration_s is not None and 0 < duration_s <= thr.short_max_seconds


def is_longform(duration_s: int | None, thr) -> bool:
    return duration_s is not None and duration_s >= thr.longform_min_seconds


def dedupe_items(items: list[ContentItem], tol_s: int = 0) -> list[ContentItem]:
    """Drop repeated item ids and re-uploads (same normalised title and ~same duration); keep earliest."""
    seen_ids: set[tuple[str, str]] = set()
    kept: list[ContentItem] = []
    for it in sorted(items, key=lambda i: i.published_at):
        if (it.platform, it.item_id) in seen_ids:
            continue
        seen_ids.add((it.platform, it.item_id))
        t = normalise(it.title or "")
        dup = t and any(
            normalise(k.title or "") == t
            and (k.duration_s or 0) - tol_s <= (it.duration_s or 0)
            <= (k.duration_s or 0) + tol_s
            for k in kept
        )
        if not dup:
            kept.append(it)
    return kept


def compute_activity(items: list[ContentItem], now: datetime, thr) -> dict:
    items = dedupe_items(items, thr.reupload_duration_tolerance_s)
    in30 = [i for i in items if within_days(i.published_at, thr.window_days_short, now)]
    in60 = [i for i in items if within_days(i.published_at, thr.window_days_long, now)]
    long60 = [i for i in in60 if is_longform(i.duration_s, thr)]
    shorts60 = [i for i in in60 if i.is_short or is_short(i.duration_s, thr)]
    latest = max(items, key=lambda i: i.published_at) if items else None
    latest_long = max(long60, key=lambda i: i.published_at) if long60 else None
    return {
        "count_30d": len(in30),
        "longform_60d": len(long60),
        "shorts_60d": len(shorts60),
        "items_60d": len(in60),
        "passed": len(in30) >= thr.active_count_30d or len(long60) >= thr.active_longform_60d,
        "last_content_date": ymd(latest.published_at) if latest else None,
        "recent_content_url": (latest_long or latest).url if latest else None,
        "recent_longform": latest_long,
        "urls": [i.url for i in sorted(in60, key=lambda i: i.published_at, reverse=True)[:5]],
        "window_end": ymd(now),
    }


def check_active(items: list[ContentItem], now: datetime, thr) -> CheckResult:
    a = compute_activity(items, now, thr)
    m = metric(count_30d=a["count_30d"], longform_60d=a["longform_60d"], window_end=a["window_end"], items=a["urls"])
    details = {k: v for k, v in a.items() if k != "recent_longform"}
    ev = a["recent_content_url"]
    if a["passed"]:
        return passed("active", evidence_url=ev, quote_or_metric=m, details=details)
    return failed("active", "inactive", evidence_url=ev, quote_or_metric=m, details=details)
