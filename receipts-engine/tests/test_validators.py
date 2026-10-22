from __future__ import annotations

import pytest

from receipts.outreach import validators as V
from receipts.outreach.validators import MessageDraft, all_pass, only_config_failures, validate

TITLE = "How to price your first offer, part 1"
LINK = "http://localhost:8000/r/abc123def4567890"


@pytest.fixture
def s(settings):
    s = settings.model_copy(deep=True)
    s.sender.name = "Sam Sample"
    s.sender.postal_address = "1 Sample Street, Testville"
    s.offer_line = "IncuBrix helps creators turn long videos into short clips."
    return s


def good(s, **kw) -> MessageDraft:
    body = (f'Hi Jo,\n\nYour 20 October video "{TITLE}" runs 25 minutes. Clips could help.\n\n{s.offer_line}\n\n'
            f"Want an example? {LINK}\n\n{s.sender.name}\n{s.sender.role}\n{s.sender.postal_address}\n"
            f"{s.outreach.opt_out_line}")
    d = dict(channel="email", step=1, body=body, subject=f"{TITLE} - an idea for clips",
             hook=f'Your 20 October video "{TITLE}" runs 25 minutes.', hook_title=TITLE,
             hook_source_url="https://y.test/v1", hook_snapshot_text=f'{{"title": "{TITLE}"}}', tracked_link=LINK,
             assisted_send=False, allowed_numbers={"20", "25", "1"})
    d.update(kw)
    return MessageDraft(**d)


def test_good_message_passes_all_rules(s):
    rep = validate(good(s), s)
    assert all_pass(rep), [r for r in rep if not r["pass"]]
    assert len(rep) == 10  # rule 6 is two functions: banned_phrases + max_links


def test_hook_in_snapshot(s):
    assert V.hook_in_snapshot(good(s), s)[0]
    assert not V.hook_in_snapshot(good(s, hook_snapshot_text="a different video title"), s)[0]


def test_sender_block(s):
    assert V.sender_block(good(s), s)[0]
    m = good(s)
    m.body = m.body.replace(s.sender.postal_address, "")
    assert not V.sender_block(m, s)[0]
    li = good(s, channel="linkedin", body=f"Hi. {s.sender.name}, {s.sender.role}. {s.outreach.linkedin_opt_out}",
              assisted_send=True)
    assert V.sender_block(li, s)[0]  # linkedin needs name and role only


def test_opt_out(s):
    assert V.opt_out(good(s), s)[0]
    m = good(s)
    m.body = m.body.replace(s.outreach.opt_out_line, "")
    assert not V.opt_out(m, s)[0]
    assert V.opt_out(good(s, channel="linkedin", body="x. not relevant? say so and I will not follow up."), s)[0]


@pytest.mark.parametrize("claim", ["I work at IncuBrix", "our team at IncuBrix", "I'm with IncuBrix"])
def test_no_employee_claim(s, claim):
    assert V.no_employee_claim(good(s), s)[0]
    m = good(s)
    m.body += f"\n{claim} and love clips."
    assert not V.no_employee_claim(m, s)[0]


def test_length(s):
    assert V.length(good(s), s)[0]
    assert not V.length(good(s, body="word " * 121), s)[0]
    assert V.length(good(s, step=3, body="word " * 60), s)[0]
    assert not V.length(good(s, step=3, body="word " * 61), s)[0]
    assert not V.length(good(s, channel="linkedin", body="x" * 301, assisted_send=True), s)[0]
    assert not V.length(good(s, channel="form", body="word " * 101, assisted_send=True), s)[0]
    # the compliance footer does not count against the copy's word budget
    footer = f"\n{s.sender.name}, {s.sender.role}\n{s.sender.postal_address}\n{s.outreach.opt_out_line}"
    assert V.length(good(s, step=3, body="word " * 55 + footer), s)[0]


def test_banned_phrases(s):
    assert V.banned_phrases(good(s), s)[0]
    m = good(s)
    m.body = "Quick question: " + m.body
    assert not V.banned_phrases(m, s)[0]


def test_max_links(s):
    assert V.max_links(good(s), s)[0]
    m = good(s)
    m.body += "\nAlso see https://other.test/x"
    ok, note = V.max_links(m, s)
    assert not ok and "2 links" in note
    one_untracked = good(s, body=good(s).body.replace(LINK, "https://other.test/x"))
    assert not V.max_links(one_untracked, s)[0]


def test_no_unsupported_numbers(s):
    assert V.no_unsupported_numbers(good(s), s)[0]
    m = good(s)
    m.body = m.body.replace("Clips could help.", "Clips could triple your 40 views.")
    ok, note = V.no_unsupported_numbers(m, s)
    assert not ok and "40" in note
    # numbers inside URLs (tracked link token) are not facts
    assert V.no_unsupported_numbers(good(s, tracked_link="http://h.test/r/999", body=good(s).body.replace(LINK, "http://h.test/r/999")), s)[0]


def test_no_todo_tokens(s):
    assert V.no_todo_tokens(good(s), s)[0]
    assert not V.no_todo_tokens(good(s, body="Hi {{ name }}"), s)[0]
    assert not V.no_todo_tokens(good(s, body="Hi TODO"), s)[0]


def test_channel_rules(s):
    assert V.channel_rules(good(s), s)[0]
    assert not V.channel_rules(good(s, channel="linkedin", assisted_send=False), s)[0]
    assert not V.channel_rules(good(s, channel="form", assisted_send=False), s)[0]
    assert V.channel_rules(good(s, channel="form", assisted_send=True), s)[0]


def test_missing_opt_out_fails_overall(s):
    m = good(s)
    m.body = m.body.replace(s.outreach.opt_out_line, "")
    rep = validate(m, s)
    assert not all_pass(rep) and not only_config_failures(rep)


def test_todo_config_is_only_a_config_failure(settings):
    m = good(settings)  # shipped settings: sender name/address are TODO
    m.body = m.body.replace("Sam Sample", "TODO").replace("1 Sample Street, Testville", "TODO")
    rep = validate(m, settings)
    assert only_config_failures(rep)
