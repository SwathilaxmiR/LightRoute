"""End-to-end replay of the synthetic world through s0..s8 + selection."""
from __future__ import annotations

from scripts.make_synthetic import default_world, input_ref


def test_full_pipeline_with_filled_config(run_world, filled_config):
    world = default_world()
    summary, db = run_world(world, [input_ref(c) for c in world])
    assert summary.status == "ok" and summary.produced >= 8
    reasons = {r["reject_reason"] for r in db.fetch_all("SELECT reject_reason FROM candidates WHERE status='rejected'")}
    assert {"excluded_keyword", "not_english"} <= reasons
    bad = db.fetch_all("SELECT creator_key, channel, step, validator_report FROM messages WHERE validated=0")
    assert not bad, bad[:1]
    assert db.scalar("SELECT COUNT(*) FROM leads WHERE priority='A'") >= 1
    # every qualified creator has 7-check material: receipts for each check family
    for (key,) in db.fetch_all("SELECT creator_key FROM creators WHERE status='qualified'"):
        names = {r[0] for r in db.fetch_all("SELECT DISTINCT check_name FROM receipts WHERE creator_key=?", (key,))}
        assert {"real_relevant", "active", "commercial", "needs", "reachable", "country", "language"} <= names


def test_unfilled_config_keeps_leads_but_unvalidated(run_world):
    world = default_world()[:4]
    summary, db = run_world(world, [input_ref(c) for c in world])
    assert summary.produced >= 2
    assert db.scalar("SELECT COUNT(*) FROM messages WHERE validated=1") == 0
    assert db.scalar("SELECT COUNT(*) FROM leads WHERE priority='A'") == 0
