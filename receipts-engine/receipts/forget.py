"""`receipts forget CREATOR_KEY`: delete a creator's data, and snapshots no other creator references."""
from __future__ import annotations

from pathlib import Path


def forget(db, creator_key: str) -> dict:
    if not db.fetch_one("SELECT 1 FROM creators WHERE creator_key=?", (creator_key,)):
        return {"found": False}
    snaps = {r[0] for r in db.fetch_all(
        "SELECT snap_id FROM receipts WHERE creator_key=? AND snap_id IS NOT NULL UNION "
        "SELECT snap_id FROM content_items WHERE creator_key=? AND snap_id IS NOT NULL UNION "
        "SELECT snap_id FROM channels WHERE creator_key=? AND snap_id IS NOT NULL UNION "
        "SELECT source_snap_id FROM identities WHERE creator_key=? AND source_snap_id IS NOT NULL",
        (creator_key,) * 4)}
    lead = db.scalar("SELECT lead_id FROM leads WHERE creator_key=?", (creator_key,))
    with db.transaction():
        db.execute("DELETE FROM content_items WHERE creator_key=?", (creator_key,))
        db.execute("DELETE FROM creators WHERE creator_key=?", (creator_key,))  # cascades receipts, channels, ...
        db.execute("UPDATE candidates SET status='rejected', reject_reason='forgotten', url='[forgotten]', "
                   "hint=NULL, creator_key=NULL WHERE creator_key=?", (creator_key,))
        if lead:
            db.execute("DELETE FROM schedule WHERE lead_id=?", (lead,))
            db.execute("DELETE FROM replies WHERE lead_id=?", (lead,))
            db.execute("INSERT OR REPLACE INTO suppressions (lead_id, reason, at) VALUES (?, 'forgotten', datetime('now'))",
                       (lead,))
    removed = 0
    for sid in snaps:
        still = db.scalar(
            "SELECT (SELECT COUNT(*) FROM receipts WHERE snap_id=?) + (SELECT COUNT(*) FROM content_items WHERE snap_id=?) "
            "+ (SELECT COUNT(*) FROM channels WHERE snap_id=?) + (SELECT COUNT(*) FROM identities WHERE source_snap_id=?)",
            (sid,) * 4)
        if still:
            continue
        row = db.fetch_one("SELECT path, sha256 FROM snapshots WHERE snap_id=?", (sid,))
        db.execute("DELETE FROM snapshots WHERE snap_id=?", (sid,))
        if row and not db.scalar("SELECT COUNT(*) FROM snapshots WHERE sha256=?", (row["sha256"],)):
            Path(row["path"]).unlink(missing_ok=True)
            removed += 1
    return {"found": True, "lead_id": lead, "snapshots_deleted": removed}
