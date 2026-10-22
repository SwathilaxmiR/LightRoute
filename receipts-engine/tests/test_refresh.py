from __future__ import annotations

from datetime import timedelta

from receipts.util.dates import utcnow
from scripts.make_synthetic import SynthCreator, default_world, input_ref

WORLD = [c for c in default_world() if c.n <= 8]


def refs(world):
    return [input_ref(c) for c in world]


def test_repeat_run_adds_nothing(run_world, filled_config):
    s1, db = run_world(WORLD, refs(WORLD))
    leads = db.fetch_all("SELECT lead_id, creator_key FROM leads ORDER BY lead_id")
    s2, db = run_world(WORLD, refs(WORLD), build=False)
    assert s2.produced == 0 and s2.new_candidates == 0 and s2.unchanged == len(WORLD)
    assert db.fetch_all("SELECT lead_id, creator_key FROM leads ORDER BY lead_id") == leads


def test_new_input_adds_only_new_creators(run_world, filled_config):
    run_world(WORLD, refs(WORLD))
    extra = [SynthCreator(n=30, kind="youtube", country="AU", place="Sydney, Australia")]
    s2, db = run_world(WORLD + extra, refs(WORLD + extra), kind="refresh")
    assert s2.new_candidates == 1 and s2.produced == 1 and s2.updated == 0
    assert db.scalar("SELECT COUNT(*) FROM creators") == len(WORLD) + 1


def test_stale_creator_reverified_not_duplicated(run_world, filled_config):
    s1, db = run_world(WORLD, refs(WORLD))
    key, lead = db.fetch_one("SELECT creator_key, lead_id FROM leads ORDER BY lead_id LIMIT 1")
    n_creators = db.scalar("SELECT COUNT(*) FROM creators")
    old = (utcnow() - timedelta(days=20)).isoformat()
    db.execute("UPDATE creators SET last_verified=? WHERE creator_key=?", (old, key))
    s2, db = run_world(WORLD, refs(WORLD), build=False, kind="refresh")
    assert s2.reverify == 1 and s2.updated == 1 and s2.produced == 0
    assert db.scalar("SELECT COUNT(*) FROM creators") == n_creators
    assert db.scalar("SELECT lead_id FROM leads WHERE creator_key=?", (key,)) == lead
    assert db.scalar("SELECT last_verified FROM creators WHERE creator_key=?", (key,)) > old


def test_creator_that_now_fails_activity_moves_to_rejects(run_world, filled_config):
    yt = [c for c in WORLD if c.kind == "youtube"][0]
    s1, db = run_world(WORLD, refs(WORLD))
    key = db.scalar("SELECT creator_key FROM identities WHERE platform='youtube' AND platform_id=?", (yt.channel_id,))
    assert db.scalar("SELECT status FROM creators WHERE creator_key=?", (key,)) == "qualified"
    yt.active = False  # the channel stopped publishing
    db.execute("UPDATE creators SET last_verified=? WHERE creator_key=?",
               ((utcnow() - timedelta(days=20)).isoformat(), key))
    s2, db = run_world(WORLD, refs(WORLD), kind="refresh")
    row = db.fetch_one("SELECT status, reject_reason FROM candidates WHERE creator_key=?", (key,))
    assert row["status"] == "rejected" and row["reject_reason"] in ("inactive", "not_creator_segment")
    assert db.scalar("SELECT status FROM leads WHERE creator_key=?", (key,)) == "Rejected"
    assert db.scalar("SELECT COUNT(*) FROM creators WHERE creator_key=?", (key,)) == 1
