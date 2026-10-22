from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from receipts import guards
from receipts.guards import (
    AssistedOnly,
    ConfigIncomplete,
    DataOriginViolation,
    ProvenanceMissing,
    RecipientNotAllowed,
    RobotsDisallowed,
    StaleEvidence,
)
from tests.conftest import ROOT

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


def _creator(db, key="cr_test"):
    db.insert("creators", {"creator_key": key, "display_name": "Test"})
    return key


# ---------------------------------------------------------------- G1
def test_g1_refuses_synthetic_origin():
    with pytest.raises(DataOriginViolation):
        guards.assert_live_origin([{"data_origin": "live"}, {"data_origin": "synthetic"}], "L0001")


def test_g1_refuses_fixture_origin():
    with pytest.raises(DataOriginViolation):
        guards.assert_live_origin([{"data_origin": "fixture"}])


def test_g1_accepts_all_live():
    guards.assert_live_origin([{"data_origin": "live"}, {"data_origin": "live"}])


# ---------------------------------------------------------------- G2
def test_g2_refuses_non_allowed_recipient():
    with pytest.raises(RecipientNotAllowed):
        guards.assert_recipient_allowed("creator@example.com", ["me@mine.test"])


def test_g2_refuses_todo_placeholder_even_if_listed():
    with pytest.raises(RecipientNotAllowed):
        guards.assert_recipient_allowed("TODO_TEST_ADDRESS_1", ["TODO_TEST_ADDRESS_1"])


def test_g2_allows_listed_and_plus_address():
    guards.assert_recipient_allowed("Me@Mine.test", ["me@mine.test"])
    guards.assert_recipient_allowed("me+tok123@mine.test", ["me@mine.test"])


# ---------------------------------------------------------------- G3
def test_g3_channel_insert_without_source_url_fails_at_db_level(db):
    key = _creator(db)
    row = {"creator_key": key, "channel_type": "email", "handle_or_url": "a@b.co",
           "source_url": None, "source_quote": "contact: a@b.co", "business_language": 1,
           "live_ok": 1, "name_match": 1, "checked_at": NOW.isoformat()}
    with pytest.raises(sqlite3.IntegrityError):
        db.insert("channels", row)


def test_g3_channel_insert_with_empty_quote_fails_at_db_level(db):
    key = _creator(db)
    row = {"creator_key": key, "channel_type": "email", "handle_or_url": "a@b.co",
           "source_url": "https://b.co/contact", "source_quote": "", "business_language": 1,
           "live_ok": 1, "name_match": 1, "checked_at": NOW.isoformat()}
    with pytest.raises(sqlite3.IntegrityError):
        db.insert("channels", row)


def test_g3_provenance_helper():
    with pytest.raises(ProvenanceMissing):
        guards.assert_contact_provenance("a@b.co", "", "a@b.co", "email")
    with pytest.raises(ProvenanceMissing):
        guards.assert_contact_provenance("a@b.co", "https://b.co", "write to us", "email")
    guards.assert_contact_provenance("a@b.co", "https://b.co", "Business: a@b.co", "email")


# ---------------------------------------------------------------- G4
def test_g4_refuses_while_todo_present(settings):
    # shipped settings.yaml still has TODO in sender/offer_line
    with pytest.raises(ConfigIncomplete) as e:
        guards.assert_config_complete(settings)
    assert "sender.name" in str(e.value) and "offer_line" in str(e.value)


def test_g4_passes_when_filled(settings):
    s = settings.model_copy(deep=True)
    s.sender.name = "Ada"
    s.sender.postal_address = "1 Road, Town"
    s.offer_line = "IncuBrix turns long videos into clips."
    guards.assert_config_complete(s)


def test_g4_extended_yaml(tmp_path):
    p = tmp_path / "submission.yaml"
    p.write_text("name: TODO\nnested:\n  email: me@x.test\n")
    assert guards.yaml_todos(p) == ["submission.yaml:name"]
    assert guards.yaml_todos(tmp_path / "missing.yaml")


# ---------------------------------------------------------------- G5
def test_g5_refuses_stale_evidence():
    with pytest.raises(StaleEvidence):
        guards.assert_fresh([NOW - timedelta(days=31)], 30, NOW)


def test_g5_accepts_boundary():
    guards.assert_fresh([NOW - timedelta(days=30), (NOW - timedelta(days=1)).isoformat()], 30, NOW)


# ---------------------------------------------------------------- G6
ROBOTS = "User-agent: *\nDisallow: /private\n"


def test_g6_robots_disallowed_refused():
    with pytest.raises(RobotsDisallowed):
        guards.assert_robots_allowed(ROBOTS, "https://site.test/private/page", "ReceiptsEngine/1.0")


def test_g6_robots_allowed_and_missing():
    guards.assert_robots_allowed(ROBOTS, "https://site.test/about", "ReceiptsEngine/1.0")
    guards.assert_robots_allowed(None, "https://site.test/private", "ReceiptsEngine/1.0")


# ---------------------------------------------------------------- G7
@pytest.mark.parametrize("ch", ["linkedin", "instagram", "x"])
def test_g7_never_auto_send_social(ch):
    with pytest.raises(AssistedOnly):
        guards.assert_auto_send_allowed(ch)
    assert guards.assisted_flag(ch)


def test_g7_email_auto_allowed():
    guards.assert_auto_send_allowed("email")
    assert not guards.assisted_flag("email")
    assert guards.assisted_flag("form")


# ---------------------------------------------------------------- G8
def test_g8_detector_finds_planted_key(tmp_path):
    fake = "AIza" + "B" * 35  # built at runtime so this test file itself never matches
    (tmp_path / "leak.py").write_text(f"KEY = '{fake}'\n")
    (tmp_path / "ok.py").write_text("x = 1\n")
    found = guards.scan_for_secrets([tmp_path / "leak.py", tmp_path / "ok.py"])
    assert found == [(str(tmp_path / "leak.py"), "google_api_key")]


def test_g8_detector_finds_env_values(tmp_path):
    env = tmp_path / ".env"
    env.write_text("YOUTUBE_API_KEY=supersecretvalue123\nSMTP_HOST=smtp.gmail.com\n")
    (tmp_path / "a.txt").write_text("leaked supersecretvalue123 here")
    (tmp_path / "b.txt").write_text("smtp.gmail.com is fine")
    vals = guards.env_secret_values(env)
    assert vals == ["supersecretvalue123"]
    found = guards.scan_for_secrets([tmp_path / "a.txt", tmp_path / "b.txt"], vals)
    assert found == [(str(tmp_path / "a.txt"), "env_value")]


def test_g8_no_secrets_in_tracked_files():
    files = guards.tracked_files(ROOT)
    assert files, "no tracked files found"
    assert not any(f.name == ".env" for f in files), ".env must never be tracked"
    findings = guards.scan_for_secrets(files, guards.env_secret_values(ROOT / ".env"))
    assert findings == [], findings


# ---------------------------------------------------------------- integration: G2 sender, G7 scheduler
def test_g2_seedtest_sender_refuses_real_creator(filled_config):
    from receipts.config import get_settings
    from receipts.outreach.seedtest import Senders

    with pytest.raises(RecipientNotAllowed):
        Senders(get_settings(), smtp_factory=object).email("creator@realcreator.example", "s", "b", "0" * 16)


def test_g7_scheduler_never_sends_linkedin(filled_config):
    from receipts.config import get_settings
    from receipts.outreach import seedtest as st
    from receipts.util.dates import utcnow

    s = get_settings()
    db = st.seed_db(s)
    now = utcnow()
    st.schedule(db, s, ["linkedin"], now)

    class Boom(st.Senders):
        def email(self, *a, **k):
            raise AssertionError("must not send")

        form = community = email

    res = st.tick(db, s, now, Boom(s))
    assert res["sent"] == 0 and res["queued_for_human"] == 5
    assert db.scalar("SELECT COUNT(*) FROM schedule WHERE status='sent'") == 0
