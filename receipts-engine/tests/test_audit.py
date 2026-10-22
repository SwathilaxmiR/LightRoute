from __future__ import annotations

import asyncio
import os
from pathlib import Path

from receipts.audit import run_audit
from receipts.config import get_settings
from receipts.context import open_context
from scripts.make_synthetic import default_world, input_ref


def audit(n=20, channels=20):
    async def go():
        async with open_context(get_settings(), llm=None) as ctx:
            return await run_audit(ctx, n, 7, channels, False)
    return asyncio.run(go())


def setup(run_world):
    world = default_world()
    run_world(world, [input_ref(c) for c in world])


def test_clean_world_passes_and_writes_reports(run_world, filled_config):
    setup(run_world)
    a = audit()
    assert a["n"] > 0 and a["pass_rate"] == 1.0 and a["channel_accuracy"] == 1.0
    s = get_settings()
    assert (s.docs_dir / "self_audit.md").read_text().startswith("# Self-audit")
    assert list((s.out_dir / "logs").glob("audit_*.json"))


def test_same_seed_same_sample(run_world, filled_config):
    setup(run_world)
    a, b = audit(n=5), audit(n=5)
    assert [x["lead_id"] for x in a["leads"]] == [x["lead_id"] for x in b["leads"]]


def test_tampered_quote_is_caught(run_world, filled_config):
    setup(run_world)
    from receipts.db import init_db

    db = init_db(get_settings().db_path)
    db.execute("UPDATE receipts SET quote_or_metric='This video is sponsored by a brand we invented' "
               "WHERE check_name='commercial' AND sub_key='sponsorship' AND quote_or_metric NOT LIKE 'paid%'")
    db.execute("UPDATE receipts SET quote_or_metric='Use code FAKE for a fake discount, invented' "
               "WHERE check_name='commercial' AND sub_key='affiliate'")
    db.execute("DELETE FROM receipts WHERE check_name='commercial' AND sub_key IN ('product_or_course','service','membership')")
    a = audit()
    assert a["per_check"]["commercial"] < a["n"] and a["pass_rate"] < 1.0
    assert any(k.startswith("commercial") for k in a["root_causes"])


def test_missing_mx_fails_channel_accuracy(run_world, filled_config):
    setup(run_world)
    for f in (Path(os.environ["RECEIPTS_FIXTURES"]) / "dns").glob("*.json"):
        f.unlink()
    a = audit()
    assert a["channel_accuracy"] < 1.0 and a["per_check"]["reachable"] < a["n"]
