"""Seed test (BUILD_SPEC 13): scheduling, tracked links and reply logging on accounts the human owns.

Never touches a real creator: 5 synthetic test leads in a separate DB (out/seedtest/state.db), every send
passes G2 (allowed_recipients), LinkedIn is queued for a human (G7). Time is compressed by
seed_test.time_compression_factor (1440: one day of follow_up_days = one minute).
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import smtplib
from dataclasses import dataclass, field
from datetime import timedelta
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path

import httpx

from receipts.db import DB, init_db
from receipts.guards import AssistedOnly, assert_auto_send_allowed, assert_recipient_allowed
from receipts.util.dates import utcnow

SEED_DB = "seedtest/state.db"
CHANNELS = ("email", "form", "linkedin", "community")
TEST_LEADS = [f"T{i:03d}" for i in range(1, 6)]
MSGID_DOMAIN = "receipts.seedtest"


def seed_db(s) -> DB:
    return init_db(s.out_dir / SEED_DB)


def token_for(lead_id: str, channel: str, step: int) -> str:
    return hashlib.sha256(f"seed:{lead_id}:{channel}:{step}".encode()).hexdigest()[:16]


def plus_address(addr: str, token: str) -> str:
    user, _, dom = addr.partition("@")
    return f"{user}+{token}@{dom}"


def allowed(s) -> list[str]:
    return [a for a in s.seed_test.allowed_recipients if a and "TODO" not in a]


def prerequisites(s) -> dict[str, list[str]]:
    """What the human still has to provide, per channel."""
    env = os.environ
    miss: dict[str, list[str]] = {c: [] for c in CHANNELS}
    if not allowed(s):
        for c in ("email", "form"):
            miss[c].append("seed_test.allowed_recipients (two mailboxes you own)")
    for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS"):
        if not env.get(k):
            miss["email"].append(k)
            miss["form"].append(k)
    if "TODO" in s.sender.reply_to:
        miss["form"].append("sender.reply_to (receives the form acknowledgement)")
    if not env.get(s.seed_test.community_webhook_env):
        miss["community"].append(s.seed_test.community_webhook_env)
    return miss


def compose(s, lead_id: str, channel: str, step: int, token: str) -> tuple[str, str]:
    link = f"{s.seed_test.tracker_base_url.rstrip('/')}/r/{token}"
    subject = f"[seed-{token}] Seed test {lead_id} step {step}"
    body = (f"Hi,\n\nThis is seed-test message {step} of 3 for synthetic test lead {lead_id} on the {channel} channel. "
            f"It goes only to a mailbox or account you own.\n\nTracked link: {link}\n\n"
            f"{s.sender.name}\n{s.sender.role}\n{s.sender.postal_address}\n{s.outreach.opt_out_line}\n")
    return subject, body


@dataclass
class Senders:
    """Real senders; tests inject fakes. Each returns True on success."""
    settings: object
    smtp_factory: object = None
    http_post: object = None
    log: list[str] = field(default_factory=list)

    def email(self, recipient: str, subject: str, body: str, token: str, in_reply_to: str | None = None) -> bool:
        s = self.settings
        assert_recipient_allowed(recipient, s.seed_test.allowed_recipients)  # G2, on every send
        env = os.environ
        if not (env.get("SMTP_HOST") and env.get("SMTP_USER") and env.get("SMTP_PASS")):
            self.log.append("email: SMTP_HOST/SMTP_USER/SMTP_PASS not set")
            return False
        msg = EmailMessage()
        msg["From"] = env["SMTP_USER"]
        msg["To"] = recipient
        msg["Subject"] = subject
        msg["Message-ID"] = make_msgid(idstring=f"seed-{token}", domain=MSGID_DOMAIN)
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = in_reply_to
        reply_to = s.sender.reply_to if "TODO" not in s.sender.reply_to else env["SMTP_USER"]
        msg["Reply-To"] = plus_address(reply_to, token)
        msg.set_content(body)
        factory = self.smtp_factory or (lambda: smtplib.SMTP(env["SMTP_HOST"], int(env.get("SMTP_PORT", 587)), timeout=30))
        with factory() as smtp:
            smtp.starttls()
            smtp.login(env["SMTP_USER"], env["SMTP_PASS"])
            smtp.send_message(msg)
        return True

    def form(self, lead_id: str, token: str, message: str) -> bool:
        url = self.settings.seed_test.tracker_base_url.rstrip("/") + "/form"
        post = self.http_post or (lambda u, data: httpx.post(u, data=data, timeout=15))
        try:
            r = post(url, {"lead_id": lead_id, "token": token, "message": message})
        except httpx.HTTPError as e:
            self.log.append(f"form: tracker not reachable at {url} ({type(e).__name__}); start `receipts tracker`")
            return False
        return 200 <= r.status_code < 300

    def community(self, lead_id: str, token: str, message: str) -> bool:
        hook = os.environ.get(self.settings.seed_test.community_webhook_env)
        if not hook:
            self.log.append(f"community: {self.settings.seed_test.community_webhook_env} not set")
            return False
        post = self.http_post or (lambda u, data: httpx.post(u, json=data, timeout=15))
        r = post(hook, {"content": f"[seed-{token}] {message}"})
        return 200 <= r.status_code < 300


def send_form_ack(s, token: str, lead_id: str, mailer=None) -> bool:
    """The tracker emails the sender a 'creator reply' to a form submission (to exercise reply logging)."""
    to = s.sender.reply_to if "TODO" not in s.sender.reply_to else os.environ.get("SMTP_USER", "")
    if not to:
        return False
    sender = mailer or Senders(s)
    try:
        return sender.email(plus_address(to, token), f"Re: [seed-{token}] form received",
                            f"Thanks for the note about {lead_id}. Yes, send the example over.\n", token,
                            in_reply_to=f"<seed-{token}@{MSGID_DOMAIN}>")
    except Exception:  # an acknowledgement failure must not break the form endpoint
        return False


def schedule(db: DB, s, channels: list[str], now) -> int:
    recips = allowed(s)
    n = 0
    with db.transaction():
        for i, lead in enumerate(TEST_LEADS):
            for ch in channels:
                if ch in ("email", "form") and not recips:
                    continue
                recipient = recips[i % len(recips)] if recips else f"{ch}:account-you-own"
                for step, days in enumerate(s.follow_up_days, 1):
                    due = now + timedelta(seconds=days * 86400 / s.seed_test.time_compression_factor)
                    n += db.insert("schedule", {"lead_id": lead, "channel": ch, "step": step, "due_at": due.isoformat(),
                                                "status": "pending", "token": token_for(lead, ch, step),
                                                "recipient": recipient}, or_ignore=True) and 1
    return n


def _sent_today(db: DB, channel: str, now) -> int:
    day = now.strftime("%Y-%m-%d")
    return db.scalar("SELECT COUNT(*) FROM schedule WHERE channel=? AND status='sent' AND substr(sent_at,1,10)=?",
                     (channel, day)) or 0


def tick(db: DB, s, now, senders: Senders) -> dict[str, int]:
    out = {"sent": 0, "queued_for_human": 0, "cancelled": 0, "failed": 0, "capped": 0}
    rows = db.fetch_all("SELECT * FROM schedule WHERE status='pending' AND due_at <= ? ORDER BY due_at, sched_id",
                        (now.isoformat(),))
    for r in rows:
        if db.fetch_one("SELECT 1 FROM suppressions WHERE lead_id=?", (r["lead_id"],)):
            db.execute("UPDATE schedule SET status='cancelled' WHERE sched_id=?", (r["sched_id"],))
            out["cancelled"] += 1
            continue
        cap = s.seed_test.daily_caps.get(r["channel"], 20)
        if _sent_today(db, r["channel"], now) >= cap:
            out["capped"] += 1
            continue
        subject, body = compose(s, r["lead_id"], r["channel"], r["step"], r["token"])
        try:
            assert_auto_send_allowed(r["channel"])  # G7: linkedin/instagram/x raise
            if r["channel"] == "email":
                ok = senders.email(r["recipient"], subject, body, r["token"])
            elif r["channel"] == "form":
                ok = senders.form(r["lead_id"], r["token"], body)
            elif r["channel"] == "community":
                ok = senders.community(r["lead_id"], r["token"], body)
            else:
                ok = False
        except AssistedOnly:
            queue_for_human(s, r, body)
            db.execute("UPDATE schedule SET status='queued_for_human' WHERE sched_id=?", (r["sched_id"],))
            out["queued_for_human"] += 1
            continue
        if ok:
            db.execute("UPDATE schedule SET status='sent', sent_at=? WHERE sched_id=?", (now.isoformat(), r["sched_id"]))
            out["sent"] += 1
        else:
            db.execute("UPDATE schedule SET status='failed' WHERE sched_id=?", (r["sched_id"],))
            out["failed"] += 1
    return out


def queue_for_human(s, r, body: str) -> Path:
    path = s.out_dir / "seedtest" / "linkedin_queue.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        if new:
            w.writerow(["lead_id", "step", "due_at", "message", "instructions"])
        w.writerow([r["lead_id"], r["step"], r["due_at"], body,
                    "Send by hand from an account you own to another account you own, then run "
                    f"`receipts log-sent {r['lead_id']} --channel {r['channel']} --step {r['step']}`; log any reply with "
                    f"`receipts log-reply {r['lead_id']} --channel {r['channel']} --label positive|negative|stop|other`."])
    return path


def suppress(db: DB, lead_id: str, reason: str, now) -> None:
    db.execute("INSERT OR IGNORE INTO suppressions (lead_id, reason, at) VALUES (?,?,?)", (lead_id, reason, now.isoformat()))
    db.execute("UPDATE schedule SET status='cancelled' WHERE lead_id=? AND status IN ('pending','queued_for_human')",
               (lead_id,))


def record_reply(db: DB, lead_id: str, channel: str, label: str, excerpt: str, source: str, now) -> None:
    db.insert("replies", {"lead_id": lead_id, "channel": channel, "received_at": now.isoformat(), "label": label,
                          "excerpt": excerpt[:300], "source": source})
    if label in ("stop", "negative"):
        suppress(db, lead_id, f"reply:{label}", now)


def apply_replies(db: DB, replies: list[dict], now) -> int:
    n = 0
    for rep in replies:
        row = db.fetch_one("SELECT lead_id, channel FROM schedule WHERE token=?", (rep["token"],))
        if not row:
            continue
        key = rep.get("message_id") or rep["token"] + rep["excerpt"][:40]
        if db.meta_get(f"reply_seen:{key}"):
            continue
        db.meta_set(f"reply_seen:{key}", "1")
        source = "form" if "form received" in rep.get("subject", "") else "imap"
        record_reply(db, row["lead_id"], row["channel"], rep["label"], rep["excerpt"], source, now)
        n += 1
    return n


def log_sent(db: DB, lead_id: str, channel: str, step: int, now) -> bool:
    cur = db.execute("UPDATE schedule SET status='sent', sent_at=? WHERE lead_id=? AND channel=? AND step=? "
                     "AND status IN ('queued_for_human','pending')", (now.isoformat(), lead_id, channel, step))
    return cur.rowcount > 0


def write_report(db: DB, s, notes: list[str]) -> Path:
    def q(sql: str, *a) -> int:
        return db.scalar(sql, a) or 0

    lines = ["# Seed test report", "",
             f"Generated {utcnow().strftime('%Y-%m-%d %H:%M UTC')}. Synthetic test leads {', '.join(TEST_LEADS)} only; "
             "every recipient is in `seed_test.allowed_recipients` (G2). LinkedIn is sent by a person (G7).", "",
             "| Channel | Scheduled | Sent | Queued for human | Cancelled | Failed | Clicks | Replies |",
             "|---|---|---|---|---|---|---|---|"]
    for ch in CHANNELS:
        sched = q("SELECT COUNT(*) FROM schedule WHERE channel=?", ch)
        if not sched:
            continue
        clicks = q("SELECT COUNT(*) FROM clicks c JOIN schedule s ON s.token=c.token WHERE s.channel=?", ch)
        lines.append(f"| {ch} | {sched} | {q('SELECT COUNT(*) FROM schedule WHERE channel=? AND status=?', ch, 'sent')} | "
                     f"{q('SELECT COUNT(*) FROM schedule WHERE channel=? AND status=?', ch, 'queued_for_human')} | "
                     f"{q('SELECT COUNT(*) FROM schedule WHERE channel=? AND status=?', ch, 'cancelled')} | "
                     f"{q('SELECT COUNT(*) FROM schedule WHERE channel=? AND status=?', ch, 'failed')} | {clicks} | "
                     f"{q('SELECT COUNT(*) FROM replies WHERE channel=?', ch)} |")
    labels = {r[0]: r[1] for r in db.fetch_all("SELECT label, COUNT(*) FROM replies GROUP BY label")}
    lines += ["", f"Replies by label: {json.dumps(labels) if labels else 'none yet'}",
              f"Suppressions applied: {q('SELECT COUNT(*) FROM suppressions')}",
              f"Form submissions received by the tracker: {q('SELECT COUNT(*) FROM form_submissions')}", "",
              "## Screenshots", "", "Add screenshots of the sent messages, the click log and the replies to "
              "`docs/seedtest_screenshots/`:", ""]
    shots = sorted(Path("docs/seedtest_screenshots").glob("*")) if Path("docs/seedtest_screenshots").exists() else []
    lines += [f"- {p}" for p in shots] or ["- (none yet)"]
    if notes:
        lines += ["", "## Notes", ""] + [f"- {n}" for n in notes]
    path = s.out_dir / "seedtest" / "seedtest_report.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    return path


def due_summary(db: DB) -> list[tuple[str, str]]:
    return [(r["channel"], r["due_at"]) for r in db.fetch_all(
        "SELECT channel, MIN(due_at) due_at FROM schedule WHERE status='pending' GROUP BY channel")]

