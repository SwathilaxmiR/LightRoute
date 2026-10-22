from __future__ import annotations

from datetime import timedelta
from email import message_from_bytes

import pytest
from fastapi.testclient import TestClient

from receipts.config import get_settings
from receipts.guards import RecipientNotAllowed
from receipts.outreach import seedtest as st
from receipts.outreach.replies import label, parse_reply
from receipts.outreach.tracker import create_app
from receipts.util.dates import utcnow


class FakeSMTP:
    sent: list = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        pass

    def login(self, u, p):
        pass

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


class Resp:
    status_code = 200


@pytest.fixture
def env(filled_config, monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.sample.test")
    monkeypatch.setenv("SMTP_USER", "me@sample.test")
    monkeypatch.setenv("SMTP_PASS", "app-password-sample")
    FakeSMTP.sent = []
    s = get_settings()
    return s, st.seed_db(s)


def senders(s, posts=None):
    posts = posts if posts is not None else []
    return st.Senders(s, smtp_factory=FakeSMTP, http_post=lambda u, d: posts.append((u, d)) or Resp())


def test_schedule_tick_sends_step1_only_and_compresses_time(env):
    s, db = env
    now = utcnow()
    assert st.schedule(db, s, ["email", "form", "linkedin"], now) == 5 * 3 * 3
    posts: list = []
    res = st.tick(db, s, now, senders(s, posts))
    assert res["sent"] == 10 and res["queued_for_human"] == 5  # email + form sent, linkedin queued (G7)
    assert len(FakeSMTP.sent) == 5 and len(posts) == 5
    msg = FakeSMTP.sent[0]
    assert msg["To"] in s.seed_test.allowed_recipients and "seed-" in msg["Message-ID"]
    assert "/r/" in msg.get_content() and s.outreach.opt_out_line in msg.get_content()
    # step 2 is due follow_up_days[1] days later, compressed: 4 days -> 4 minutes
    later = now + timedelta(minutes=s.follow_up_days[1], seconds=1)
    assert st.tick(db, s, later, senders(s))["sent"] == 10
    q = (s.out_dir / "seedtest" / "linkedin_queue.csv").read_text()
    assert "Send by hand" in q and "T001" in q


def test_daily_cap_respected(env):
    s, db = env
    s = s.model_copy(deep=True)
    s.seed_test.daily_caps["email"] = 2
    now = utcnow()
    st.schedule(db, s, ["email"], now)
    res = st.tick(db, s, now, senders(s))
    assert res["sent"] == 2 and res["capped"] == 3


def test_stop_reply_suppresses_and_cancels(env):
    s, db = env
    now = utcnow()
    st.schedule(db, s, ["email"], now)
    st.tick(db, s, now, senders(s))
    tok = db.scalar("SELECT token FROM schedule WHERE lead_id='T001' AND step=1")
    raw = (f"From: me2@sample.test\r\nTo: me+{tok}@sample.test\r\nSubject: Re: hello\r\n"
           f"In-Reply-To: <seed-{tok}@receipts.seedtest>\r\n\r\nPlease STOP emailing me.\r\n> old text\r\n").encode()
    rep = parse_reply(raw)
    assert rep["token"] == tok and rep["label"] == "stop"
    assert st.apply_replies(db, [rep], now) == 1
    assert st.apply_replies(db, [rep], now) == 0  # idempotent
    assert db.scalar("SELECT COUNT(*) FROM schedule WHERE lead_id='T001' AND status='cancelled'") == 2
    later = now + timedelta(minutes=10)
    assert st.tick(db, s, later, senders(s))["sent"] == 8  # T002..T005 steps 2 and 3 only


@pytest.mark.parametrize("text,headers,want", [
    ("Yes, tell me more", {}, "positive"), ("No thanks", {}, "negative"), ("unsubscribe", {}, "stop"),
    ("I am away", {"Subject": "Out of office"}, "auto_reply"), ("hmm", {"Auto-Submitted": "auto-replied"}, "auto_reply"),
    ("who is this?", {}, "other"),
])
def test_reply_labels(text, headers, want):
    assert label(text, headers) == want


def test_tracker_click_and_form(env):
    s, db = env
    acks = []

    class Mailer:
        def email(self, to, subject, body, token, in_reply_to=None):
            acks.append((to, subject, in_reply_to))
            return True

    client = TestClient(create_app(s, mailer=Mailer()), follow_redirects=False)
    r = client.get("/r/abcd1234abcd1234", headers={"user-agent": "test-agent"})
    assert r.status_code == 302 and r.headers["location"] == s.seed_test.redirect_target
    row = db.fetch_one("SELECT * FROM clicks")
    assert row["token"] == "abcd1234abcd1234" and row["ua_hash"] and "test-agent" not in row["ua_hash"]
    r = client.post("/form", content="lead_id=T001&token=feedbeeffeedbeef&message=hello",
                    headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.json()["stored"] and r.json()["ack_emailed"]
    assert db.scalar("SELECT COUNT(*) FROM form_submissions") == 1
    assert "feedbeeffeedbeef" in acks[0][1] and acks[0][0].startswith("sam+feedbeeffeedbeef@")


def test_form_ack_respects_g2(filled_config, monkeypatch):
    s = get_settings()
    s = s.model_copy(deep=True)
    s.sender.reply_to = "someone-else@example.com"  # not an allowed recipient
    monkeypatch.setenv("SMTP_HOST", "h")
    monkeypatch.setenv("SMTP_USER", "u@sample.test")
    monkeypatch.setenv("SMTP_PASS", "p")
    assert st.send_form_ack(s, "0" * 16, "T001", mailer=st.Senders(s, smtp_factory=FakeSMTP)) is False
    with pytest.raises(RecipientNotAllowed):
        st.Senders(s, smtp_factory=FakeSMTP).email("someone-else@example.com", "x", "y", "0" * 16)


def test_manual_log_reply_and_report(env):
    s, db = env
    now = utcnow()
    st.schedule(db, s, ["linkedin"], now)
    st.tick(db, s, now, senders(s))
    assert st.log_sent(db, "T002", "linkedin", 1, now)
    st.record_reply(db, "T002", "linkedin", "positive", "logged manually", "manual", now)
    path = st.write_report(db, s, ["note"])
    text = path.read_text()
    assert "| linkedin | 15 | 1 | 4 |" in text and '"positive": 1' in text


def test_raw_message_roundtrip_has_reply_to_plus_token(env):
    s, db = env
    st.Senders(s, smtp_factory=FakeSMTP).email("me@sample.test", "subj", "body", "ab" * 8)
    m = message_from_bytes(FakeSMTP.sent[0].as_bytes())
    assert m["Reply-To"] == f"sam+{'ab' * 8}@sample.test"
