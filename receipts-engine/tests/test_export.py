from __future__ import annotations

import asyncio
import csv
import json
from datetime import timedelta

import pytest

from receipts.config import get_settings
from receipts.context import open_context
from receipts.guards import ConfigIncomplete
from receipts.models import LEAD_COLUMNS
from receipts.pipeline.s9_export import export_all
from receipts.util.dates import utcnow
from scripts.make_synthetic import default_world, input_ref


def run_export(**kw):
    async def go():
        async with open_context(get_settings(), llm=None) as ctx:
            return await export_all(ctx, **kw)
    return asyncio.run(go())


def simulate_live(db):
    """TEST ONLY: relabel this temp DB's synthetic evidence as live so the real (non-demo) writer path runs.
    Production data can only become 'live' by being fetched live; G1 tests below cover the refusal."""
    db.execute("UPDATE snapshots SET data_origin='live'")
    db.execute("UPDATE receipts SET data_origin='live'")


@pytest.fixture
def world_db(run_world, filled_config):
    world = default_world()
    summary, db = run_world(world, [input_ref(c) for c in world])
    assert summary.produced >= 12
    return db


def test_leads_csv_contract(world_db):
    simulate_live(world_db)
    res = run_export()
    path = get_settings().out_dir / "leads.csv"
    raw = path.read_text(encoding="utf-8")
    assert not raw.startswith("﻿")
    with path.open(encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f))
    assert rows[0] == LEAD_COLUMNS and len(LEAD_COLUMNS) == 30
    data = [dict(zip(rows[0], r, strict=True)) for r in rows[1:]]
    assert len(data) == res.written > 0
    assert [d["lead_id"] for d in data] == sorted(d["lead_id"] for d in data)
    for d in data:
        assert d["candidate_status"] == "Qualified" and d["duplicate_check"] == "UNIQUE"
        assert d["completeness_check"] == "COMPLETE" and d["primary_language"] == "en"
        assert "\\n" not in d["first_message"] and "\n" in d["first_message"]  # real newlines, no literal \n
        empty = [c for c in LEAD_COLUMNS if c != "rejection_reason" and not d[c].strip()]
        assert not empty, (d["lead_id"], empty)
        assert d["priority"] in "ABC" and float(d["lead_score"]) == round(float(d["lead_score"]), 1)
        assert d["creator_segment"] in ("Podcaster", "YouTuber", "Expert-led creator")
    n_db = world_db.scalar("SELECT COUNT(*) FROM leads WHERE status='Qualified' AND first_exported_at IS NOT NULL")
    assert n_db == len(data)
    # companion outputs
    out = get_settings().out_dir
    assert (out / "rejects.csv").read_text().startswith("candidate_ref,platform,source_id,url,stage,reject_reason")
    crm = list(csv.DictReader((out / "crm_export.csv").open(encoding="utf-8")))
    assert len(crm) == len(data) and crm[0]["status"] == "New" and crm[0]["opt_out_status"] == "none"
    assert crm[0]["source_basis"].startswith("public business contact, source: http")
    rj = json.loads((out / "receipts" / f"{data[0]['lead_id']}.json").read_text())
    assert set(rj["checks"]) >= {"real_relevant", "active", "commercial", "needs", "reachable", "personalised",
                                 "complete_unique"}
    assert rj["checks"]["complete_unique"]["result"] == "pass"
    assert (out / "outreach" / "validator_report.csv").exists() and (out / "viewer" / "index.html").exists()
    assert len(list((out / "outreach" / "sequences").rglob("sequence.md"))) == 9
    assert list((out / "outreach" / "priority_a").glob("L*.md"))


def test_g1_export_refuses_synthetic_origin(world_db):
    res = run_export()
    assert res.written == 0 and res.dropped.get("origin_not_live", 0) > 0
    with (get_settings().out_dir / "leads.csv").open() as f:
        assert len(list(csv.reader(f))) == 1  # header only


def test_demo_export_never_writes_leads_csv(world_db):
    res = run_export(demo=True)
    out = get_settings().out_dir
    assert res.written > 0 and (out / "leads_DEMO_not_live.csv").exists() and not (out / "leads.csv").exists()


def test_g4_export_refuses_todo_config(run_world):
    world = default_world()[:3]
    run_world(world, [input_ref(c) for c in world])
    with pytest.raises(ConfigIncomplete):
        run_export()


def test_g5_stale_evidence_dropped_to_rejects(world_db):
    simulate_live(world_db)
    victim = world_db.scalar("SELECT creator_key FROM leads ORDER BY lead_id LIMIT 1")
    old = (utcnow() - timedelta(days=45)).isoformat()
    world_db.execute("UPDATE receipts SET checked_at=? WHERE creator_key=?", (old, victim))
    res = run_export(allow_imbalance=True)
    assert res.dropped.get("stale_evidence") == 1
    rej = list(csv.DictReader((get_settings().out_dir / "rejects.csv").open()))
    assert any(r["reject_reason"] == "stale_evidence" for r in rej)


def test_trace_prints_seven_checks(world_db, capsys):
    from rich.console import Console

    from receipts.trace import print_trace

    simulate_live(world_db)
    run_export()
    con = Console(width=200, record=True)
    print_trace(world_db, get_settings(), "L0001", console=con)
    text = con.export_text()
    for label in ("real_relevant", "active", "commercial", "needs", "reachable", "personalised", "complete+unique"):
        assert label in text
    assert "PASS" in text


def test_forget_removes_creator(world_db):
    from receipts.forget import forget

    key = world_db.scalar("SELECT creator_key FROM leads ORDER BY lead_id LIMIT 1")
    res = forget(world_db, key)
    assert res["found"] and not world_db.scalar("SELECT COUNT(*) FROM receipts WHERE creator_key=?", (key,))
    assert not world_db.scalar("SELECT COUNT(*) FROM content_items WHERE creator_key=?", (key,))
    assert world_db.scalar("SELECT COUNT(*) FROM suppressions WHERE lead_id=?", (res["lead_id"],)) == 1
