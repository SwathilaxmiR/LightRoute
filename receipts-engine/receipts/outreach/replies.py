"""Reply labelling and IMAP polling for the seed test (BUILD_SPEC 13)."""
from __future__ import annotations

import email
import imaplib
import os
import re
from email.header import decode_header, make_header
from email.message import Message

STOP = re.compile(r"\b(stop|unsubscribe|remove me)\b", re.I)
POSITIVE = re.compile(r"\b(interested|yes|sure|send|tell me more)\b", re.I)
NEGATIVE = re.compile(r"\b(no thanks|not interested)\b", re.I)
TOKEN = re.compile(r"seed-([0-9a-f]{16})")
PLUS = re.compile(r"\+([0-9a-f]{16})@")


def label(text: str, headers: dict[str, str] | None = None) -> str:
    h = {k.lower(): v for k, v in (headers or {}).items()}
    if STOP.search(text or ""):
        return "stop"
    if (h.get("auto-submitted", "no").lower() not in ("", "no") or "x-autoreply" in h
            or re.search(r"out of office", h.get("subject", ""), re.I)):
        return "auto_reply"
    if NEGATIVE.search(text or ""):
        return "negative"
    if POSITIVE.search(text or ""):
        return "positive"
    return "other"


def _body(msg: Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
        return ""
    payload = msg.get_payload(decode=True) or b""
    return payload.decode(msg.get_content_charset() or "utf-8", "replace")


def strip_quoted(text: str) -> str:
    """Only the new reply text: drop quoted lines and everything after 'On ... wrote:'."""
    out = []
    for line in text.splitlines():
        if line.startswith(">") or re.match(r"^On .+ wrote:$", line.strip()):
            break
        out.append(line)
    return "\n".join(out).strip()


def match_token(msg: Message) -> str | None:
    for h in ("In-Reply-To", "References", "Subject"):
        m = TOKEN.search(str(msg.get(h, "")))
        if m:
            return m.group(1)
    for h in ("To", "Delivered-To", "X-Original-To"):
        m = PLUS.search(str(msg.get(h, "")))
        if m:
            return m.group(1)
    return None


def parse_reply(raw: bytes) -> dict | None:
    msg = email.message_from_bytes(raw)
    tok = match_token(msg)
    if not tok:
        return None
    text = strip_quoted(_body(msg))
    subject = str(make_header(decode_header(msg.get("Subject", ""))))
    headers = {"Subject": subject, **{k: str(v) for k, v in msg.items() if k.lower() in
                                      ("auto-submitted", "x-autoreply")}}
    return {"token": tok, "label": label(text, headers), "excerpt": text[:300], "subject": subject,
            "message_id": msg.get("Message-ID", "")}


def poll_imap(since_days: int = 2) -> list[dict]:
    """Fetch recent INBOX messages that answer a seed-test message. Requires IMAP_HOST/USER/PASS."""
    host, user, pw = os.environ.get("IMAP_HOST"), os.environ.get("IMAP_USER"), os.environ.get("IMAP_PASS")
    if not (host and user and pw):
        raise RuntimeError("IMAP_HOST, IMAP_USER and IMAP_PASS must be set in .env to poll replies")
    import datetime as dt

    since = (dt.date.today() - dt.timedelta(days=since_days)).strftime("%d-%b-%Y")
    out = []
    with imaplib.IMAP4_SSL(host) as m:
        m.login(user, pw)
        m.select("INBOX", readonly=True)
        _, ids = m.search(None, "SINCE", since)
        for i in ids[0].split():
            _, data = m.fetch(i, "(RFC822)")
            if data and isinstance(data[0], tuple):
                r = parse_reply(data[0][1])
                if r:
                    out.append(r)
    return out
