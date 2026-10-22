"""s8: draft and validate outreach messages from stored facts. Rendered from the DB so `export` can
re-render after the human fills sender/offer settings (messages drafted while they say TODO stay validated=0)."""
from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache

from jinja2 import Environment, PackageLoader, StrictUndefined

from receipts.checks.c6_personalised import personalised_result
from receipts.guards import assisted_flag
from receipts.llm.client import HOOK_SCHEMA
from receipts.outreach.hooks import (
    HookContext,
    build_hook,
    first_name,
    load_context,
    second_observation,
    signal_phrase,
)
from receipts.outreach.validators import NUMBER, MessageDraft, all_pass, only_config_failures, validate
from receipts.pipeline.common import Reject, save_receipt
from receipts.pipeline.runner import register_stage
from receipts.util.dates import date_words, parse_dt, utcnow

MSG_CHANNEL = {"email": "email", "youtube": "email", "form": "form", "website": "form",
               "linkedin": "linkedin", "instagram": "linkedin", "x": "linkedin"}
SEGMENTS = ("podcaster", "youtuber", "expert_led")
MSG_CHANNELS = ("email", "linkedin", "form")


def message_channel(channel_type: str) -> str:
    return MSG_CHANNEL.get(channel_type, "email")


@lru_cache(maxsize=1)
def env() -> Environment:
    e = Environment(loader=PackageLoader("receipts.outreach", "templates"), undefined=StrictUndefined,
                    keep_trailing_newline=False, autoescape=False)
    e.filters["ucfirst"] = lambda v: v[:1].upper() + v[1:] if v else v
    return e


def tracked_link(s, key: str, channel: str, step: int) -> str:
    tok = hashlib.sha256(f"{key}:{channel}:{step}".encode()).hexdigest()[:16]
    return f"{s.seed_test.tracker_base_url.rstrip('/')}/r/{tok}"


def allowed_numbers(db, key: str, s) -> set[str]:
    """Numbers a message may contain: from receipts, content items, and the human-written sender/offer text."""
    nums: set[str] = set()
    for r in db.fetch_all("SELECT quote_or_metric FROM receipts WHERE creator_key=?", (key,)):
        nums |= set(NUMBER.findall(re.sub(r"https?://\S+", " ", r["quote_or_metric"] or "")))
    for r in db.fetch_all("SELECT title, duration_s, published_at FROM content_items WHERE creator_key=?", (key,)):
        nums |= set(NUMBER.findall(r["title"] or ""))
        if r["duration_s"]:
            nums |= {str(r["duration_s"]), str(round(r["duration_s"] / 60))}
        dt = parse_dt(r["published_at"])
        if dt:
            nums |= {str(dt.day), str(dt.year), f"{dt.day:02d}"}
    for txt in (s.sender.postal_address, s.sender.role, s.sender.name, s.offer_line):
        nums |= set(NUMBER.findall(txt or ""))
    return nums


def _vars(ctx: HookContext, s, key: str, channel: str, step: int, db) -> dict:
    it = ctx.item
    title = it.title
    return {
        "creator_first_name": first_name(ctx.name), "creator_name": ctx.name, "hook": ctx.hook,
        "hook_short": ctx.hook_short, "signal_phrase": signal_phrase(ctx), "item_word": ctx.item_word,
        "offer_line": s.offer_line, "sender": s.sender, "tracked_link": tracked_link(s, key, channel, step),
        "opt_out_line": s.outreach.opt_out_line, "linkedin_opt_out": s.outreach.linkedin_opt_out,
        "subject_title": title if len(title) <= 60 else title[:57].rstrip() + "...", "title": title,
        "date_words": date_words(parse_dt(it.published)), "second_observation": second_observation(ctx, db),
    }


def render(ctx: HookContext, s, key: str, channel: str, step: int, db, store) -> MessageDraft:
    v = _vars(ctx, s, key, channel, step, db)
    text = env().get_template(f"{ctx.segment}/{channel}/step{step}.j2").render(**v).strip()
    if channel == "linkedin" and step == 1 and len(text) > s.message_limits.linkedin_chars_first and v["hook_short"]:
        v["hook_short"] = ""  # shorter template-only variant; the title stays exact
        text = env().get_template(f"{ctx.segment}/{channel}/step{step}.j2").render(**v).strip()
    subject = None
    if text.startswith("Subject:"):
        first, _, rest = text.partition("\n")
        subject, text = first[len("Subject:"):].strip(), rest.strip()
    snap_text = store.load_snapshot_text(ctx.item.snap_id) if ctx.item.snap_id else ""
    return MessageDraft(channel=channel, step=step, body=text, subject=subject, hook=ctx.hook, hook_title=ctx.item.title,
                        hook_source_url=ctx.item.url, hook_snapshot_text=snap_text,
                        tracked_link=v["tracked_link"] if v["tracked_link"] in text else "",
                        assisted_send=assisted_flag(channel, s.channels.assisted_types) or channel == "form",
                        allowed_numbers=allowed_numbers(db, key, s))


def polish(ctx: HookContext, llm, s) -> None:
    """Optional LLM rewrite of the hook; kept only if the exact title survives and no new digits appear."""
    if llm is None:
        return
    out = llm.judge("polish_hook", {"hook": ctx.hook, "title": ctx.item.title, "date": ctx.item.published[:10]},
                    HOOK_SCHEMA).get("hook", "")
    new_digits = set(NUMBER.findall(out)) - set(NUMBER.findall(ctx.hook))
    if out and ctx.item.title in out and not new_digits and len(out.split()) <= s.outreach.hook_max_words:
        ctx.hook = out


def store_message(db, key: str, m: MessageDraft, report: list[dict]) -> None:
    db.upsert("messages", ["creator_key", "channel", "step"], {
        "creator_key": key, "channel": m.channel, "step": m.step, "subject": m.subject, "body": m.body, "hook": m.hook,
        "hook_source_url": m.hook_source_url, "validated": int(all_pass(report)), "validator_report": json.dumps(report),
        "assisted_send": int(m.assisted_send), "created_at": utcnow().isoformat()})


def plan(db, key: str, s) -> dict[str, int]:
    """Which message channels and how many steps: best (+backup) channel; 3 steps if A-eligible, else step 1."""
    from receipts.pipeline.s7_score import a_eligible

    best = db.fetch_one("SELECT channel_type FROM channels WHERE creator_key=? AND role='best'", (key,))
    backup = db.fetch_one("SELECT channel_type FROM channels WHERE creator_key=? AND role='backup'", (key,))
    if not best:
        return {}
    steps = 3 if a_eligible(db, key, s) else 1
    out = {message_channel(best["channel_type"]): steps}
    if steps == 3 and backup:
        out.setdefault(message_channel(backup["channel_type"]), 3)
    return out


class NoHookItem(Exception):
    pass


def draft_for_creator(db, store, s, key: str, now, llm=None) -> tuple[bool, bool]:
    """Returns (ok, config_todo). ok=False means a real validation failure after the template-only retry.
    Raises NoHookItem when the creator has no recent titled item to personalise from."""
    ctx = load_context(db, key, s, now)
    if ctx is None:
        raise NoHookItem(key)
    build_hook(ctx)
    template_hook = ctx.hook
    polish(ctx, llm, s)
    config_todo = False
    reports: list[list[dict]] = []
    with db.transaction():
        db.execute("DELETE FROM messages WHERE creator_key=?", (key,))
        for ch, n in plan(db, key, s).items():
            for step in range(1, n + 1):
                m = render(ctx, s, key, ch, step, db, store)
                rep = validate(m, s)
                if not all_pass(rep) and ctx.hook != template_hook:
                    ctx.hook = template_hook  # regenerate once from template only (no LLM)
                    m = render(ctx, s, key, ch, step, db, store)
                    rep = validate(m, s)
                reports.append(rep)
                if not all_pass(rep):
                    if only_config_failures(rep):
                        config_todo = True
                    else:
                        store_message(db, key, m, rep)
                        save_receipt(db, key, personalised_result(ctx.hook, ctx.item.url, ctx.item.snap_id, reports,
                                                                  False), "live")
                        return False, False
                store_message(db, key, m, rep)
        save_receipt(db, key, personalised_result(ctx.hook, ctx.item.url, ctx.item.snap_id, reports, config_todo),
                     "live")
    return True, config_todo


@register_stage("s8")
async def messages_stage(ctx, c, key: str, fr, origin: str) -> None:
    try:
        ok, todo = draft_for_creator(ctx.db, ctx.store, ctx.settings, key, ctx.now, ctx.llm)
    except NoHookItem as e:
        raise Reject("no_hook_item", "no recent titled non-Short item", "messaged") from e
    if not ok:
        raise Reject("message_validation_failed", stage="messaged")
    if todo:
        ctx.bump("messages_config_todo")


def rerender_all(db, store, s, now, llm=None) -> dict[str, int]:
    """Re-draft every qualified lead's messages with the current settings (used by export)."""
    out = {"ok": 0, "failed": 0, "config_todo": 0}
    for r in db.fetch_all("SELECT l.creator_key FROM leads l JOIN creators c USING(creator_key) "
                          "WHERE c.status='qualified' AND l.status IN ('Qualified','Reserve')"):
        try:
            ok, todo = draft_for_creator(db, store, s, r["creator_key"], now, llm)
        except NoHookItem:
            ok, todo = False, False
        out["ok" if ok and not todo else "config_todo" if ok else "failed"] += 1
    return out
