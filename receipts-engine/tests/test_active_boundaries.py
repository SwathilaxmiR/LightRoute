from __future__ import annotations

from datetime import UTC, datetime, timedelta

from receipts.checks.c2_active import check_active, compute_activity, is_longform, is_short
from receipts.models import ContentItem
from receipts.util.dates import parse_iso_duration, within_days

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


def item(i, days_ago, dur=300, title=None, short=False):
    return ContentItem(platform="youtube", item_id=f"v{i}", url=f"https://www.youtube.com/watch?v=v{i}",
                       title=title or f"Video {i}", published_at=NOW - timedelta(days=days_ago),
                       duration_s=dur, is_short=short)


def test_7_vs_8_items_in_30_days(settings):
    thr = settings.thresholds
    seven = [item(i, i + 1) for i in range(7)]
    eight = [item(i, i + 1) for i in range(8)]
    assert compute_activity(seven, NOW, thr)["passed"] is False
    assert compute_activity(eight, NOW, thr)["passed"] is True


def test_item_exactly_30_days_old_counts_31_does_not(settings):
    thr = settings.thresholds
    assert within_days(NOW - timedelta(days=30), 30, NOW)
    assert not within_days(NOW - timedelta(days=31), 30, NOW)
    base = [item(i, i + 1) for i in range(7)]
    assert compute_activity(base + [item(99, 30)], NOW, thr)["count_30d"] == 8
    assert compute_activity(base + [item(99, 31)], NOW, thr)["count_30d"] == 7


def test_1_vs_2_longform_in_60_days(settings):
    thr = settings.thresholds
    one = [item(1, 10, dur=1200)]
    two = [item(1, 10, dur=1200), item(2, 59, dur=1500)]
    assert compute_activity(one, NOW, thr)["passed"] is False
    a = compute_activity(two, NOW, thr)
    assert a["passed"] is True and a["longform_60d"] == 2


def test_longform_479_vs_480(settings):
    thr = settings.thresholds
    assert not is_longform(479, thr)
    assert is_longform(480, thr)
    assert compute_activity([item(1, 5, 479), item(2, 6, 479)], NOW, thr)["longform_60d"] == 0
    assert compute_activity([item(1, 5, 480), item(2, 6, 480)], NOW, thr)["longform_60d"] == 2


def test_shorts_60_vs_61(settings):
    thr = settings.thresholds
    assert is_short(60, thr)
    assert not is_short(61, thr)
    assert compute_activity([item(1, 5, 60)], NOW, thr)["shorts_60d"] == 1
    assert compute_activity([item(1, 5, 61)], NOW, thr)["shorts_60d"] == 0


def test_reuploaded_duplicates_counted_once(settings):
    thr = settings.thresholds
    items = [item(i, i + 1) for i in range(7)]
    reupload = item(50, 2, dur=300, title="Video 3")  # same title + duration as v3, new id
    same_id = item(3, 4)
    a = compute_activity(items + [reupload, same_id], NOW, thr)
    assert a["count_30d"] == 7 and a["passed"] is False


def test_check_active_metric_string(settings):
    r = check_active([item(1, 1, 900), item(2, 3, 900)], NOW, settings.thresholds)
    assert r.result == "pass"
    assert r.quote_or_metric.startswith("count_30d=2; longform_60d=2; window_end=2026-10-21; items=")
    assert r.evidence_url == "https://www.youtube.com/watch?v=v1"


def test_iso_duration():
    assert parse_iso_duration("PT1H2M3S") == 3723
    assert parse_iso_duration("PT8M") == 480
    assert parse_iso_duration("P0D") == 0
    assert parse_iso_duration("P1DT1S") == 86401
    assert parse_iso_duration("garbage") == 0
