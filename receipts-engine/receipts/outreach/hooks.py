"""Personalisation hooks built only from stored facts (content_items + receipts). No invented details."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from receipts.checks.c4_needs import SIGNALS
from receipts.db import DB
from receipts.util.dates import date_words, parse_dt, within_days

SIGNAL_PHRASES = {
    "no_shortform": "a few short clips of your longer pieces could reach people who never watch a full {item_word}",
    "captions_gap": "captions on your recent {item_word}s would make them easier to watch with sound off",
    "backlog": "your back catalogue holds a lot of material that could be turned into new clips and posts",
    "inconsistent_publishing": "a steadier publishing rhythm is easier to keep when clips and posts are prepared in batches",
    "team_workflow": "splitting repetitive editing steps can free up time for the parts only you can do",
    "single_platform": "short cuts of your {item_word}s could carry your work to platforms you are not on yet",
}
PODCAST_SIGNAL_PHRASES = {
    "captions_gap": "transcripts for your recent {item_word}s would make them easier to search and quote",
}


@dataclass
class HookItem:
    title: str
    url: str
    published: str
    duration_s: int | None
    platform: str
    has_captions: int | None
    snap_id: int | None


@dataclass
class HookContext:
    creator_key: str
    name: str
    segment: str
    item: HookItem
    item_word: str
    signals: list[str]                       # passing need signals, strongest first
    metrics: dict[str, dict[str, str]]       # sub_key -> parsed metric dict
    hook: str = ""
    hook_short: str = ""
    signal: str = ""
    extra: dict = field(default_factory=dict)


def parse_metric(m: str | None) -> dict[str, str]:
    out = {}
    for part in (m or "").split("; "):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def pick_item(db: DB, key: str, s, now) -> HookItem | None:
    """Most recent long-form item within hook_recent_days, else within the 60-day window; creators who publish
    shorter pieces (often expert-led) fall back to their most recent non-Short item in the same windows."""
    for min_secs in (s.thresholds.longform_min_seconds, s.thresholds.short_max_seconds + 1):
        rows = db.fetch_all("SELECT * FROM content_items WHERE creator_key=? AND duration_s >= ? "
                            "ORDER BY published_at DESC", (key, min_secs))
        for days in (s.outreach.hook_recent_days, s.thresholds.window_days_long):
            for r in rows:
                if r["title"] and within_days(parse_dt(r["published_at"]), days, now):
                    return HookItem(r["title"], r["url"], r["published_at"], r["duration_s"], r["platform"],
                                    r["has_captions"], r["snap_id"])
    return None


def load_context(db: DB, key: str, s, now) -> HookContext | None:
    cr = db.fetch_one("SELECT * FROM creators WHERE creator_key=?", (key,))
    item = pick_item(db, key, s, now)
    if cr is None or item is None:
        return None
    rows = db.fetch_all("SELECT sub_key, quote_or_metric FROM receipts WHERE creator_key=? AND check_name='needs' "
                        "AND result='pass' AND sub_key != ''", (key,))
    metrics = {r["sub_key"]: parse_metric(r["quote_or_metric"]) for r in rows}
    strong = {"no_shortform", "backlog"} | ({"captions_gap"} if s.evidence.captions_gap_strength == "strong" else set())
    order = sorted(metrics, key=lambda k: (k not in strong, SIGNALS.index(k) if k in SIGNALS else 99))
    word = "episode" if item.platform == "podcast" or cr["segment"] == "podcaster" else "video"
    return HookContext(key, cr["display_name"], cr["segment"] or "youtuber", item, word, order, metrics)


def _mins(sec: int | None) -> int | None:
    return round(sec / 60) if sec else None


def build_hook(ctx: HookContext) -> tuple[str, str]:
    """(hook_text, hook_source_url). Every fact comes from ctx.item or a receipt metric."""
    it = ctx.item
    d = date_words(parse_dt(it.published))
    mins = _mins(it.duration_s)
    base = f'Your {d} {ctx.item_word} "{it.title}"' + (f" runs {mins} minutes" if mins else "")
    sig = ctx.signals[0] if ctx.signals else ""
    if sig == "no_shortform" and it.platform == "youtube":
        hook = base + ", and I could not find shorter cuts of it on your channel."
        short = "I could not find shorter cuts of it on your channel."
    elif sig == "captions_gap" and it.has_captions == 0:
        if it.platform == "podcast":
            hook = base + ", and I could not find a transcript for it in your feed."
            short = "I could not find a transcript for it in your feed."
        else:
            hook = base + ", and the captions option shows as unavailable on it."
            short = "The captions option shows as unavailable on it."
    elif sig == "backlog" and (n := ctx.metrics.get("backlog", {}).get("public_videos")
                               or ctx.metrics.get("backlog", {}).get("episodes")):
        items = "videos" if "public_videos" in ctx.metrics.get("backlog", {}) else "episodes"
        hook = f'With {n} {items} published, your {d} {ctx.item_word} "{it.title}" is one of many long pieces to draw from.'
        short = f"With {n} {items} published, there is a lot to draw from."
    else:
        hook = base + "."
        short = ""
        sig = sig or ""
    ctx.hook, ctx.hook_short, ctx.signal = hook, short, sig
    return hook, it.url


def signal_phrase(ctx: HookContext) -> str:
    table = dict(SIGNAL_PHRASES)
    if ctx.item.platform == "podcast":
        table.update(PODCAST_SIGNAL_PHRASES)
    sig = ctx.signal if ctx.signal in table else (ctx.signals[0] if ctx.signals and ctx.signals[0] in table else "no_shortform")
    return table[sig].format(item_word=ctx.item_word)


def second_observation(ctx: HookContext, db: DB) -> str:
    """A fact from a different receipt than the hook's signal; else a cadence fact."""
    for sig in ctx.signals:
        if sig == ctx.signal:
            continue
        m = ctx.metrics.get(sig, {})
        if sig == "captions_gap" and m.get("missing") and m.get("sampled"):
            what = "transcripts are missing" if "transcript" in m.get("field", "") else "captions show as unavailable"
            return f"{what} on {m['missing']} of the last {m['sampled']} long {ctx.item_word}s"
        if sig == "backlog" and (m.get("public_videos") or m.get("episodes")):
            n = m.get("public_videos") or m.get("episodes")
            return f"you have {n} {'videos' if m.get('public_videos') else 'episodes'} published, which is a large library to cut from"
        if sig == "inconsistent_publishing" and m.get("max_gap_days") not in (None, "na"):
            return f"the longest gap between uploads in the last 90 days was {round(float(m['max_gap_days']))} days"
        if sig == "no_shortform":
            return "I could not find any short clips on the channel in the last 60 days"
        if sig == "team_workflow":
            return "your notes mention a production team, so clips could slot into a workflow you already have"
        if sig == "single_platform":
            return "your work currently lives mainly on one platform"
    act = db.fetch_one("SELECT quote_or_metric FROM receipts WHERE creator_key=? AND check_name='active'", (ctx.creator_key,))
    m = parse_metric(act["quote_or_metric"] if act else "")
    n = m.get("count_30d", "0")
    return f"you published {n} {ctx.item_word}{'' if n == '1' else 's'} in the last 30 days"


SHOW_WORDS = re.compile(r"\b(podcast|show|channel|tv|studio|studios|media|academy|radio|network|club|hq|official|"
                        r"daily|weekly|the|and|&|with|co|inc|ltd|llc|synthetic)\b", re.I)


def first_name(name: str) -> str:
    """A person's first name when the display name looks like a person; else '<Name> team'."""
    toks = name.split()
    if 2 <= len(toks) <= 3 and not SHOW_WORDS.search(name) and all(t[:1].isupper() and t.isalpha() for t in toks):
        return toks[0]
    return f"{name} team"
