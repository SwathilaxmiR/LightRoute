from __future__ import annotations

import json
import random

from receipts.pipeline.s7_score import (
    _violations,
    balanced_selection,
    fit_size,
    lead_score,
    select_final,
    upsert_lead,
)
from tests.helpers import NOW

SEGS = ["podcaster", "youtuber", "expert_led"]


def synth_rows(n=1000, us=0.8, countries=("GB", "CA", "AU", "IE", "NZ", "SG"), seed=1):
    rnd = random.Random(seed)
    rows = []
    for i in range(n):
        c = "US" if rnd.random() < us else rnd.choice(countries)
        rows.append({"lead_id": f"L{i + 1:04d}", "creator_key": f"cr_{i}", "score": round(rnd.uniform(40, 99), 1),
                     "country": c, "segment": SEGS[i % 3]})
    return rows


def test_us_skew_capped_at_55_percent(settings):
    sel, viol = balanced_selection(synth_rows(), settings.quotas_final)
    assert not viol and len(sel) > 300
    us = sum(r["country"] == "US" for r in sel) / len(sel)
    assert us <= settings.quotas_final.max_country_share


def test_fewer_than_3_countries_reports_violation(settings):
    rows = synth_rows(200, us=0.5, countries=("GB",))
    sel, viol = balanced_selection(rows, settings.quotas_final)
    assert any("countries" in v for v in viol)


def test_segment_tolerance_enforced(settings):
    q = settings.quotas_final
    rows = synth_rows(900)
    for r in rows[:600]:
        r["segment"] = "youtuber"  # heavy skew to one segment
    sel, viol = balanced_selection(rows, q)
    assert not viol
    for seg, t in q.segment_targets.items():
        share = sum(r["segment"] == seg for r in sel) / len(sel)
        assert abs(share - t) <= q.segment_tolerance + 1e-9
    assert _violations([{"country": "US", "segment": "youtuber"}] * 10, q)


def test_fit_size_taper(settings):
    assert fit_size(None, settings) == settings.fit_size.unknown_value
    assert fit_size(20000, settings) == 1.0
    assert fit_size(0, settings) == settings.fit_size.taper_floor
    assert 0.3 < fit_size(2_000_000, settings) < 1.0
    assert fit_size(5_000_000, settings) == settings.fit_size.taper_floor


def test_lead_score_weighting(settings):
    comp = {k: 1.0 for k in settings.scoring.weights}
    assert lead_score(comp, settings) == 100.0
    comp["need"] = 0.0
    assert lead_score(comp, settings) == 70.0


def _seed_creator(db, i, country, segment, *, verified=True, validated=True):
    key = f"cr_{i:04d}"
    db.insert("creators", {"creator_key": key, "display_name": f"Synthetic {i}", "segment": segment,
                           "country_iso2": country, "status": "qualified", "followers": 20000})
    for sub, m in (("sponsorship", "q one"), ("affiliate", "q two")):
        db.insert("receipts", {"creator_key": key, "check_name": "commercial", "sub_key": sub, "result": "pass",
                               "quote_or_metric": m, "verified_by": "rule", "checked_at": NOW.isoformat()})
    for sub in ("no_shortform", "single_platform"):
        db.insert("receipts", {"creator_key": key, "check_name": "needs", "sub_key": sub, "result": "pass",
                               "quote_or_metric": "x=1", "verified_by": "rule", "checked_at": NOW.isoformat()})
    db.insert("receipts", {"creator_key": key, "check_name": "active", "sub_key": "", "result": "pass",
                           "quote_or_metric": f"count_30d={i % 16}; longform_60d=3", "verified_by": "rule",
                           "checked_at": NOW.isoformat()})
    for role, typ in (("best", "email"), ("backup", "form")):
        db.insert("channels", {"creator_key": key, "channel_type": typ, "handle_or_url": f"{typ}{i}",
                               "source_url": "https://s.test", "source_quote": "business inquiries", "business_language": 1,
                               "live_ok": 1, "name_match": 1, "verified": int(verified), "role": role,
                               "checked_at": NOW.isoformat()})
    for step in (1, 2, 3):
        db.insert("messages", {"creator_key": key, "channel": "email", "step": step, "body": "b", "hook": "h",
                               "hook_source_url": "u", "validated": int(validated), "created_at": NOW.isoformat()})
    return key


def test_priority_a_count_equals_target_and_ids_stable(settings, db, tmp_path):
    cs = ["US", "GB", "CA", "AU"]
    keys = [_seed_creator(db, i, cs[i % 4], SEGS[i % 3]) for i in range(60)]
    ids1 = {k: upsert_lead(db, k, settings, NOW)[0] for k in keys}
    bal = select_final(db, settings, tmp_path / "balance.json")
    assert bal.priority.get("A") == settings.priority.target_count
    assert not bal.violations
    assert json.loads((tmp_path / "balance.json").read_text())["selected"] == bal.selected
    # re-scoring and re-selection keep every lead id
    ids2 = {k: upsert_lead(db, k, settings, NOW)[0] for k in keys}
    select_final(db, settings)
    assert ids1 == ids2 and sorted(ids1.values())[0] == "L0001"
    assert db.meta_get("lead_seq") == "60"


def test_priority_a_requires_validated_messages(settings, db):
    keys = [_seed_creator(db, i, ["US", "GB", "CA"][i % 3], SEGS[i % 3], validated=False) for i in range(30)]
    for k in keys:
        upsert_lead(db, k, settings, NOW)
    assert select_final(db, settings).priority.get("A", 0) == 0
