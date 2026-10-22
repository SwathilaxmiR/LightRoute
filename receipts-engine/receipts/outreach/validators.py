"""Message validators (BUILD_SPEC 8.10 s8). Each returns (pass, note)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from receipts.util.text import normalise, word_count

EMPLOYEE_CLAIM = re.compile(r"\b(i work at incubrix|our team at incubrix|i'?m with incubrix|i am with incubrix"
                            r"|we at incubrix|on behalf of incubrix)\b", re.I)
URL = re.compile(r"https?://\S+")
NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


@dataclass
class MessageDraft:
    channel: str                 # email | linkedin | form
    step: int
    body: str
    subject: str | None
    hook: str
    hook_title: str
    hook_source_url: str
    hook_snapshot_text: str
    tracked_link: str = ""
    assisted_send: bool = False
    allowed_numbers: set[str] = field(default_factory=set)


def hook_in_snapshot(m: MessageDraft, s) -> tuple[bool, str]:
    ok = bool(m.hook_title) and normalise(m.hook_title) in normalise(m.hook_snapshot_text)
    return ok, "" if ok else f"title {m.hook_title!r} not found in snapshot of {m.hook_source_url}"


def sender_block(m: MessageDraft, s) -> tuple[bool, str]:
    b = m.body
    need = [s.sender.name, s.sender.role] + ([] if m.channel == "linkedin" else [s.sender.postal_address])
    missing = [x for x in need if not x or "TODO" in x or x not in b]
    return not missing, "" if not missing else f"missing sender fields: {missing}"


def opt_out(m: MessageDraft, s) -> tuple[bool, str]:
    line = s.outreach.linkedin_opt_out if m.channel == "linkedin" else s.outreach.opt_out_line
    ok = normalise(line) in normalise(m.body)
    return ok, "" if ok else "opt-out sentence missing"


def no_employee_claim(m: MessageDraft, s) -> tuple[bool, str]:
    hit = EMPLOYEE_CLAIM.search(m.body)
    return hit is None, "" if hit is None else f"employment claim: {hit.group(0)!r}"


def copy_text(m: MessageDraft, s) -> str:
    """The message copy without the mandatory compliance footer (sender block and opt-out line), which
    sender_block/opt_out require separately and which must not crowd out the copy's word budget."""
    t = m.body
    for part in (s.outreach.opt_out_line, s.outreach.linkedin_opt_out, s.sender.postal_address, s.sender.role,
                 s.sender.name):
        if part:
            t = t.replace(part, " ")
    return t


def length(m: MessageDraft, s) -> tuple[bool, str]:
    lim = s.message_limits
    if m.channel == "linkedin" and m.step == 1:
        n, cap, unit = len(m.body), lim.linkedin_chars_first, "chars (whole note: platform limit)"
    else:
        first = lim.form_words_first if m.channel == "form" else lim.email_words_first
        cap = {1: first, 2: lim.email_words_followup, 3: lim.email_words_close}[m.step]
        n, unit = word_count(copy_text(m, s)), "words excluding sender block and opt-out"
    return n <= cap, f"{n}/{cap} {unit}"


def banned_phrases(m: MessageDraft, s) -> tuple[bool, str]:
    text = normalise((m.subject or "") + " " + m.body)
    hits = [p for p in s.banned.get("banned", []) if normalise(p) in text]
    return not hits, "" if not hits else f"banned: {hits}"


def max_links(m: MessageDraft, s) -> tuple[bool, str]:
    links = URL.findall(m.body)
    cap = s.banned.get("max_links_per_message", 1)
    if len(links) > cap:
        return False, f"{len(links)} links (max {cap})"
    stray = [u for u in links if m.tracked_link and u.rstrip(".,)") != m.tracked_link]
    return not stray, "" if not stray else f"untracked link {stray[0]}"


def no_unsupported_numbers(m: MessageDraft, s) -> tuple[bool, str]:
    text = URL.sub(" ", (m.subject or "") + " " + m.body)
    allowed = set(m.allowed_numbers) | set(NUMBER.findall(m.hook))
    bad = [n for n in NUMBER.findall(text) if n not in allowed]
    return not bad, "" if not bad else f"unsupported numbers: {sorted(set(bad))}"


def no_todo_tokens(m: MessageDraft, s) -> tuple[bool, str]:
    text = (m.subject or "") + m.body
    bad = [t for t in ("TODO", "{{", "}}", "{%", "%}") if t in text]
    return not bad, "" if not bad else f"tokens: {bad}"


def channel_rules(m: MessageDraft, s) -> tuple[bool, str]:
    if m.channel in ("linkedin", "form") and not m.assisted_send:
        return False, f"{m.channel} must be assisted_send"
    return True, ""


RULES = [hook_in_snapshot, sender_block, opt_out, no_employee_claim, length, banned_phrases, max_links,
         no_unsupported_numbers, no_todo_tokens, channel_rules]
RULE_NAMES = [r.__name__ for r in RULES]
CONFIG_RULES = {"sender_block", "no_todo_tokens"}  # failures that only mean "settings still contain TODO"


def validate(m: MessageDraft, s) -> list[dict]:
    out = []
    for rule in RULES:
        ok, note = rule(m, s)
        out.append({"rule": rule.__name__, "pass": bool(ok), "note": note})
    return out


def all_pass(report: list[dict]) -> bool:
    return all(r["pass"] for r in report)


def only_config_failures(report: list[dict]) -> bool:
    fails = {r["rule"] for r in report if not r["pass"]}
    return bool(fails) and fails <= CONFIG_RULES
