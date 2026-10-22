"""`receipts outreach-check`: all 3 segments x 3 channels x 3 steps exist and validate on a synthetic sample."""
from __future__ import annotations

from importlib import resources

from receipts.guards import config_todos
from receipts.outreach.hooks import HookContext, HookItem
from receipts.outreach.validators import MessageDraft, all_pass, validate
from receipts.pipeline.s8_messages import MSG_CHANNELS, SEGMENTS, _vars, env

SAMPLE_TITLE = "Sample episode: pricing your first offer"


class _NoDB:
    def fetch_one(self, *a, **k):
        return None


def sample_context(segment: str) -> HookContext:
    item = HookItem(SAMPLE_TITLE, "https://example.test/item", "2026-10-14T10:00:00+00:00", 1500,
                    "podcast" if segment == "podcaster" else "youtube", 0, None)
    ctx = HookContext("cr_sample", "Sample Creator", segment, item, "episode" if segment == "podcaster" else "video",
                      ["no_shortform", "captions_gap"], {"captions_gap": {"missing": "3", "sampled": "5"}})
    ctx.hook = f'Your 14 October {ctx.item_word} "{SAMPLE_TITLE}" runs 25 minutes, and I could not find shorter cuts of it on your channel.'
    ctx.hook_short = "I could not find shorter cuts of it on your channel."
    ctx.signal = "no_shortform"
    return ctx


def run_check(s) -> tuple[list[dict], bool, bool]:
    """Rows of {segment, channel, step, exists, valid, failures}; (rows, all_ok, used_sample_sender)."""
    s = s.model_copy(deep=True)
    sample_sender = bool(config_todos(s))
    if sample_sender:
        s.sender.name = "Sample Sender"
        s.sender.postal_address = "1 Sample Street, Sample Town"
        s.offer_line = "Sample offer line used only by outreach-check."
    root = resources.files("receipts.outreach").joinpath("templates")
    rows, ok = [], True
    for seg in SEGMENTS:
        ctx = sample_context(seg)
        for ch in MSG_CHANNELS:
            for step in (1, 2, 3):
                exists = root.joinpath(seg, ch, f"step{step}.j2").is_file()
                row = {"segment": seg, "channel": ch, "step": step, "exists": exists, "valid": False, "failures": ""}
                if exists:
                    v = _vars(ctx, s, "cr_sample", ch, step, _NoDB())
                    text = env().get_template(f"{seg}/{ch}/step{step}.j2").render(**v).strip()
                    subj = None
                    if text.startswith("Subject:"):
                        first, _, text = text.partition("\n")
                        subj, text = first[8:].strip(), text.strip()
                    m = MessageDraft(channel=ch, step=step, body=text, subject=subj, hook=ctx.hook,
                                     hook_title=SAMPLE_TITLE, hook_source_url=ctx.item.url,
                                     hook_snapshot_text=SAMPLE_TITLE, tracked_link=v["tracked_link"] if v["tracked_link"] in text else "",
                                     assisted_send=ch in ("linkedin", "form"),
                                     allowed_numbers={"14", "25", "3", "5", "1"})
                    rep = validate(m, s)
                    row["valid"] = all_pass(rep)
                    row["failures"] = "; ".join(f"{r['rule']}: {r['note']}" for r in rep if not r["pass"])
                ok = ok and row["exists"] and row["valid"]
                rows.append(row)
    return rows, ok, sample_sender
