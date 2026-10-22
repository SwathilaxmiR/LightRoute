"""Shared stage helpers."""
from __future__ import annotations

from receipts.models import CheckResult
from receipts.util.dates import utcnow

ORIGIN_RANK = {"live": 0, "fixture": 1, "synthetic": 2}


class Reject(Exception):
    """Permanent, expected rejection of a candidate with a reason code."""

    def __init__(self, reason: str, detail: str = "", stage: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail
        self.stage = stage


def worst_origin(db, snap_ids) -> str:
    ids = [i for i in set(snap_ids) if i]
    if not ids:
        return "live"
    rows = db.fetch_all(f"SELECT DISTINCT data_origin FROM snapshots WHERE snap_id IN ({','.join('?' * len(ids))})", ids)
    return max((r["data_origin"] for r in rows), key=lambda o: ORIGIN_RANK.get(o, 3), default="live")


def snap_origin(db, snap_id: int | None, default: str) -> str:
    if not snap_id:
        return default
    v = db.scalar("SELECT data_origin FROM snapshots WHERE snap_id=?", (snap_id,))
    return v or default


def save_receipt(db, creator_key: str, r: CheckResult, origin: str, checked_at: str | None = None) -> None:
    """Upsert one receipt row. A receipt's origin is the worse of its own snapshot and the creator's data."""
    o = snap_origin(db, r.snap_id, origin)
    o = max(o, origin, key=lambda x: ORIGIN_RANK.get(x, 3))
    db.upsert("receipts", ["creator_key", "check_name", "sub_key"], {
        "creator_key": creator_key, "check_name": r.check_name, "sub_key": r.sub_key, "result": r.result,
        "evidence_url": r.evidence_url, "quote_or_metric": r.quote_or_metric, "snap_id": r.snap_id,
        "data_origin": o, "verified_by": r.verified_by, "checked_at": checked_at or utcnow().isoformat(),
        "reason_code": r.reason_code,
    })
