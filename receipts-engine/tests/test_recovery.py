from __future__ import annotations

import json
import os

from receipts.http import write_fixture_file
from receipts.sources.youtube import BASE, VIDEO_PARTS_FULL
from scripts.make_synthetic import default_world, input_ref

WORLD = [c for c in default_world() if c.kind == "youtube"][:6]


def _break_videos(c, body: str, status: int):
    """Replace one creator's videos.list fixture with an error response."""
    ids = ",".join(f"synv{c.n:04d}{i:03d}" for i in range(30))
    write_fixture_file(os.environ["RECEIPTS_FIXTURES"], f"{BASE}/videos",
                       {"part": VIDEO_PARTS_FULL, "id": ids, "maxResults": 50}, body, status=status)


def _assert_clean(db, n):
    assert db.scalar("SELECT COUNT(*) FROM candidates WHERE platform != 'input'") == n  # nothing lost
    assert db.scalar("SELECT COUNT(*) FROM candidates WHERE status IN ('open','deferred')") == 0
    assert db.scalar("SELECT COUNT(*) - COUNT(DISTINCT creator_key) FROM creators") == 0
    assert db.scalar("SELECT COUNT(*) FROM (SELECT platform, platform_id FROM identities GROUP BY 1, 2 "
                     "HAVING COUNT(DISTINCT creator_key) > 1)") == 0
    assert db.scalar("SELECT COUNT(*) - COUNT(DISTINCT creator_key) FROM leads") == 0


def test_quota_exceeded_mid_run_then_resume(run_world, filled_config):
    refs = [input_ref(c) for c in WORLD]
    run_world(WORLD, refs, stop_after="s1")  # builds fixtures, ingests nothing to process yet
    _break_videos(WORLD[2], json.dumps({"error": {"code": 403, "errors": [{"reason": "quotaExceeded"}]}}), 403)
    s1, db = run_world(WORLD, refs, build=False)
    assert s1.status == "partial" and s1.deferred >= 1
    assert "--resume" in s1.message and "quota" in s1.message.lower()
    assert db.scalar("SELECT status FROM runs WHERE run_id=?", (s1.run_id,)) == "partial"
    assert db.scalar("SELECT COUNT(*) FROM candidates WHERE status='deferred'") == s1.deferred
    # next UTC day (fresh quota) and a working response
    db.execute("DELETE FROM meta WHERE k LIKE 'yt_units:%'")
    run_world(WORLD, [], stop_after="s1")  # rebuild good fixtures
    s2, db = run_world(WORLD, refs, build=False, resume=True)
    assert s2.status == "ok"
    rejected = db.scalar("SELECT COUNT(*) FROM candidates WHERE status='rejected'")
    assert s1.produced + s2.produced + rejected == len(WORLD)  # every candidate accounted for exactly once
    _assert_clean(db, len(WORLD))


def test_bad_key_then_resume(run_world, filled_config):
    refs = [input_ref(c) for c in WORLD]
    run_world(WORLD, refs, stop_after="s1")
    _break_videos(WORLD[1], json.dumps({"error": {"code": 400, "message": "API key not valid. Please pass a valid API key.",
                                                  "errors": [{"reason": "keyInvalid"}]}}), 400)
    s1, db = run_world(WORLD, refs, build=False)
    assert s1.status == "partial" and "API key" in s1.message and "--resume" in s1.message
    run_world(WORLD, [], stop_after="s1")  # key fixed: responses work again
    s2, db = run_world(WORLD, refs, build=False, resume=True)
    assert s2.status == "ok"
    _assert_clean(db, len(WORLD))


def test_crash_mid_candidate_resume_leaves_no_duplicates(run_world, filled_config):
    refs = [input_ref(c) for c in WORLD]
    s1, db = run_world(WORLD, refs)
    n_creators = db.scalar("SELECT COUNT(*) FROM creators")
    # simulate a kill after collection: candidate left open mid-pipeline
    db.execute("UPDATE candidates SET status='open', stage='collected' WHERE cand_id=(SELECT MIN(cand_id) FROM candidates)")
    s2, db = run_world(WORLD, refs, build=False, resume=True)
    assert s2.dupes_removed == 0 and db.scalar("SELECT COUNT(*) FROM creators") == n_creators
    _assert_clean(db, len(WORLD))
