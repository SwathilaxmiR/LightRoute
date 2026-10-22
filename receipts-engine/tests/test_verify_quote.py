from __future__ import annotations

import json

from receipts.cache import SnapshotStore
from receipts.util.text import normalise, verify_quote

PAGE = "Welcome!  For business inquiries:   hello@janedoe.co.uk.\nWe’re “building” a show — every week."


def test_exact_substring_passes():
    assert verify_quote("For business inquiries: hello@janedoe.co.uk", PAGE)


def test_smart_quotes_and_whitespace_pass_after_normalisation():
    assert verify_quote('We\'re "building" a show - every week', PAGE)
    assert verify_quote("For   business\ninquiries:  hello@janedoe.co.uk", PAGE)
    assert verify_quote("FOR BUSINESS INQUIRIES", PAGE)


def test_paraphrase_fails():
    assert not verify_quote("For commercial enquiries: hello@janedoe.co.uk", PAGE)
    assert not verify_quote("we are building a show every week", PAGE)


def test_quote_shorter_than_12_chars_fails():
    assert len(normalise("hello@jane")) < 12
    assert not verify_quote("hello@jane", PAGE)
    assert verify_quote("hello@janedoe", PAGE)  # 13 chars


def test_quote_from_a_different_snapshot_fails(db, tmp_path):
    store = SnapshotStore(db, tmp_path / "snaps")
    a = store.save_snapshot("https://a.test", b"<html><body>Sponsored by Acme Widgets this week</body></html>",
                            200, "text/html")
    b = store.save_snapshot("https://b.test", b"<html><body>Join the membership on Patreon</body></html>",
                            200, "text/html")
    q = "Sponsored by Acme Widgets"
    assert verify_quote(q, store.load_snapshot_text(a))
    assert not verify_quote(q, store.load_snapshot_text(b))


def test_json_snapshot_quotes_verify_across_newlines(db, tmp_path):
    store = SnapshotStore(db, tmp_path / "snaps")
    body = json.dumps({"items": [{"snippet": {"description": "Line one.\nBusiness: jo@x.test\nThanks"}}]})
    sid = store.save_snapshot("https://api.test/x", body.encode(), 200, "application/json")
    assert verify_quote("Line one. Business: jo@x.test", store.load_snapshot_text(sid))


def test_html_snapshot_exposes_hrefs(db, tmp_path):
    store = SnapshotStore(db, tmp_path / "snaps")
    html = b'<html><body><a href="mailto:press@show.test">Email us</a><script>var x="hidden secret";</script></body></html>'
    sid = store.save_snapshot("https://show.test/contact", html, 200, "text/html")
    text = store.load_snapshot_text(sid)
    assert verify_quote("mailto:press@show.test", text)
    assert "hidden secret" not in text


def test_identical_content_deduped_on_disk(db, tmp_path):
    store = SnapshotStore(db, tmp_path / "snaps")
    a = store.save_snapshot("https://a.test", b"same body here", 200, "text/plain")
    b = store.save_snapshot("https://a.test", b"same body here", 200, "text/plain")
    c = store.save_snapshot("https://c.test", b"same body here", 200, "text/plain")
    assert a == b and c != a
    files = list((tmp_path / "snaps").rglob("*"))
    assert sum(1 for f in files if f.is_file()) == 1
