"""`receipts manual-check`: record human proof that a LinkedIn/Instagram/X profile is the creator's and active."""
from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

from receipts.db import DB
from receipts.util.dates import parse_dt, within_days


class ManualCheckError(Exception):
    pass


def resolve_creator(db: DB, ref: str) -> str:
    if ref.startswith("cr_"):
        if db.fetch_one("SELECT 1 FROM creators WHERE creator_key=?", (ref,)):
            return ref
        raise ManualCheckError(f"unknown creator {ref}")
    key = db.scalar("SELECT creator_key FROM leads WHERE lead_id=?", (ref,))
    if not key:
        raise ManualCheckError(f"unknown lead {ref}")
    return key


def record_manual_check(db: DB, s, ref: str, channel: str, active_date: str, screenshot: Path, note: str, now) -> dict:
    if channel not in ("linkedin", "instagram", "x"):
        raise ManualCheckError("manual checks are for linkedin, instagram or x")
    key = resolve_creator(db, ref)
    row = db.fetch_one("SELECT * FROM channels WHERE creator_key=? AND channel_type=? ORDER BY channel_score DESC",
                       (key, channel))
    if row is None:
        raise ManualCheckError(f"{ref} has no {channel} channel found on a creator-owned page; nothing to check")
    screenshot = Path(screenshot)
    if not screenshot.is_file():
        raise ManualCheckError(f"screenshot not found: {screenshot}")
    d = date.fromisoformat(active_date)
    if d > now.date():
        raise ManualCheckError("active date is in the future")
    dest = s.out_dir / "manual_checks" / f"{key}_{channel}{screenshot.suffix.lower()}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(screenshot, dest)
    active = within_days(parse_dt(d.isoformat()), s.channels.active_window_days, now)
    verified = int(bool(row["live_ok"] and row["name_match"] and active))
    quote = row["source_quote"] + (f" | manual note: {note}" if note else "")
    with db.transaction():
        db.execute("UPDATE channels SET manual_check_path=?, last_active=?, active_proof='manual', verified=?, "
                   "source_quote=?, checked_at=? WHERE channel_id=?",
                   (str(dest), d.isoformat(), verified, quote, now.isoformat(), row["channel_id"]))
    from receipts.pipeline.s6_channels import reselect

    best, backup = reselect(db, key)
    return {"creator_key": key, "channel_id": row["channel_id"], "verified": bool(verified),
            "name_match": bool(row["name_match"]), "active": active, "screenshot": str(dest),
            "best": best and best["channel_type"], "backup": backup and backup["channel_type"]}
