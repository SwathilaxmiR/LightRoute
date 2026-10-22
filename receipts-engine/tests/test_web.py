from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from receipts.config import get_settings
from receipts.db import init_db
from receipts.web.app import create_app, highlight
from scripts.make_synthetic import default_world, input_ref

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 64).decode()


def simulate_live():
    """TEST ONLY: relabel this temp DB's synthetic evidence and runs as live, so the console's real-data views
    have something to show. Production data becomes live only by being fetched live."""
    db = init_db(get_settings().db_path)
    db.execute("UPDATE snapshots SET data_origin='live'")
    db.execute("UPDATE receipts SET data_origin='live'")
    db.execute("UPDATE runs SET data_origin='live'")
    db.close()


@pytest.fixture
def demo_client(run_world, filled_config):
    world = default_world()
    run_world(world, [input_ref(c) for c in world])
    return TestClient(create_app(get_settings()))


@pytest.fixture
def client(demo_client):
    simulate_live()
    return demo_client


def test_demo_data_is_never_shown(demo_client):
    dash = demo_client.get("/").text
    assert "No real data yet" in dash and "Hidden: 22 demo/fixture creators" in dash  # 18 qualified + 4 rejected
    leads = demo_client.get("/leads").text
    assert "No leads match" in leads and "Synthetic Creator" not in leads
    assert demo_client.get("/leads/L0001").status_code == 404
    assert "Synthetic" not in demo_client.get("/rejects").text
    assert "No runs yet" in demo_client.get("/runs").text
    db = init_db(get_settings().db_path)
    sid = db.scalar("SELECT snap_id FROM snapshots LIMIT 1")
    db.close()
    assert demo_client.get(f"/snapshots/{sid}").status_code == 404


def test_stats_live_only_by_default(demo_client):
    from receipts.stats import compute

    db = init_db(get_settings().db_path)
    s = get_settings()
    assert compute(db, s).qualified == 0 and compute(db, s).rejected == 0 and compute(db, s).runs == []
    assert compute(db, s, live_only=False).qualified == 18
    db.close()


def test_mixed_db_shows_only_live_creators(demo_client):
    db = init_db(get_settings().db_path)
    key = db.scalar("SELECT creator_key FROM leads WHERE lead_id='L0001'")
    db.execute("UPDATE receipts SET data_origin='live' WHERE creator_key=?", (key,))
    db.close()
    page = demo_client.get("/leads").text
    assert "L0001" in page and "L0002" not in page


def first_lead(status="Qualified"):
    db = init_db(get_settings().db_path)
    try:
        return db.scalar("SELECT lead_id FROM leads WHERE status=? ORDER BY lead_id LIMIT 1", (status,))
    finally:
        db.close()


def test_dashboard_shows_rules_readiness_and_demo_banner(client):
    r = client.get("/")
    assert r.status_code == 200
    for text in ("Acceptance rules", "A1", "Ready for a real run?", "YOUTUBE_API_KEY in .env", "Countries", "Recent runs"):
        assert text in r.text
    assert "Hidden:" not in r.text  # everything in this DB counts as live
    assert "supersecret" not in r.text


def test_leads_list_filters_and_pages(client):
    r = client.get("/leads")
    assert r.status_code == 200 and "L0001" in r.text
    assert client.get("/leads?segment=podcaster").text.count("Podcaster") >= 1
    none = client.get("/leads?q=no-such-creator-xyz")
    assert "No leads match" in none.text
    assert client.get("/leads?status=all&sort=name&page=1").status_code == 200


def test_lead_detail_has_seven_checks_messages_and_channels(client):
    lid = first_lead()
    r = client.get(f"/leads/{lid}")
    assert r.status_code == 200
    for title in ("Real and relevant", "Active", "Commercial", "Needs IncuBrix", "Reachable", "Personalised",
                  "Complete and unique", "Score breakdown", "Channels", "Messages", "Validator report"):
        assert title in r.text, title
    assert "view snapshot" in r.text and "data-copy" in r.text


def test_snapshot_page_highlights_quote_and_escapes_html(client):
    db = init_db(get_settings().db_path)
    sid = db.scalar("SELECT snap_id FROM receipts WHERE check_name='reachable' AND snap_id IS NOT NULL LIMIT 1")
    quote = db.scalar("SELECT quote_or_metric FROM receipts WHERE snap_id=? AND check_name='reachable'", (sid,))
    db.close()
    r = client.get(f"/snapshots/{sid}", params={"q": quote})
    assert r.status_code == 200 and 'id="hit"' in r.text and "Quote found" in r.text
    miss = client.get(f"/snapshots/{sid}", params={"q": "this sentence is not in the page at all"})
    assert "Quote not found verbatim" in miss.text


def test_highlight_escapes_third_party_markup():
    out = str(highlight('<script>alert(1)</script> Business: a@b.co', "Business: a@b.co"))
    assert "<script>" not in out and "&lt;script&gt;" in out and '<mark id="hit">' in out


def test_manual_check_via_console_verifies_linkedin(client):
    db = init_db(get_settings().db_path)
    lead = db.fetch_one("SELECT lead_id, creator_key FROM leads ORDER BY lead_id LIMIT 1")
    name = db.scalar("SELECT display_name FROM creators WHERE creator_key=?", (lead["creator_key"],))
    slug = name.lower().replace(" ", "-")
    db.insert("channels", {"creator_key": lead["creator_key"], "channel_type": "linkedin",
                           "handle_or_url": f"https://www.linkedin.com/in/{slug}", "source_url": "https://site.test",
                           "source_quote": f"https://www.linkedin.com/in/{slug}", "business_language": 0, "live_ok": 1,
                           "name_match": 1, "verified": 0, "role": "candidate", "checked_at": "2026-10-21T00:00:00+00:00"})
    db.close()
    page = client.get(f"/leads/{lead['lead_id']}").text
    assert "Record manual check" in page
    from receipts.util.dates import utcnow

    r = client.post("/api/manual-check", json={"lead_id": lead["lead_id"], "channel": "linkedin",
                                               "active_date": utcnow().date().isoformat(), "note": "weekly posts",
                                               "filename": "shot.png", "screenshot_b64": PNG})
    assert r.status_code == 200 and r.json()["ok"] and r.json()["verified"]
    db = init_db(get_settings().db_path)
    row = db.fetch_one("SELECT verified, active_proof, manual_check_path FROM channels WHERE channel_type='linkedin'")
    db.close()
    assert row["verified"] == 1 and row["active_proof"] == "manual" and row["manual_check_path"].endswith(".png")


@pytest.mark.parametrize("payload,err", [
    ({"screenshot_b64": "%%%"}, "base64"),
    ({"screenshot_b64": PNG, "filename": "x.exe"}, ".png"),
    ({"screenshot_b64": PNG, "filename": "x.png", "channel": "email"}, "linkedin"),
])
def test_manual_check_rejects_bad_input(client, payload, err):
    body = {"lead_id": first_lead(), "channel": "linkedin", "active_date": "2026-10-01", **payload}
    r = client.post("/api/manual-check", json=body)
    assert r.status_code == 400 and err in r.json()["error"]


def test_rejects_runs_and_seedtest_pages(client):
    r = client.get("/rejects")
    assert r.status_code == 200 and "excluded_keyword" in r.text
    assert "not_english" in client.get("/rejects?reason=not_english").text
    assert "Run ledger" in client.get("/runs").text
    s = client.get("/seedtest")
    assert s.status_code == 200 and "Still needed" in s.text


def test_seed_log_endpoints(client):
    from receipts.outreach import seedtest as st
    from receipts.util.dates import utcnow

    s = get_settings()
    st.schedule(st.seed_db(s), s, ["linkedin"], utcnow())
    st.tick(st.seed_db(s), s, utcnow(), st.Senders(s))
    assert client.post("/api/seed/log", json={"action": "sent", "lead_id": "T001", "channel": "linkedin", "step": 1}).json()["ok"]
    assert client.post("/api/seed/log", json={"action": "reply", "lead_id": "T001", "channel": "linkedin",
                                              "label": "stop"}).json()["ok"]
    assert client.post("/api/seed/log", json={"action": "reply", "lead_id": "X999", "channel": "linkedin",
                                              "label": "stop"}).status_code == 400
    page = client.get("/seedtest").text
    assert "T001 (reply:stop)" in page


def test_unknown_lead_and_snapshot_404(client):
    assert client.get("/leads/L9999").status_code == 404
    assert "Not found" in client.get("/snapshots/999999").text


def test_static_assets_served(client):
    assert "--accent" in client.get("/static/app.css").text
    assert "manual-check" in client.get("/static/app.js").text
