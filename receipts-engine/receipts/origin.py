"""What counts as real data, as SQL fragments shared by stats, the console and the audit.

- A creator is real only if every receipt it has came from a live fetch (data_origin='live').
- A candidate or run is real only if its run was a live run (runs.data_origin='live'); runs recorded before
  that column existed have NULL and are treated as not real.
"""
from __future__ import annotations


def creator_is_live(alias: str = "c") -> str:
    return (f"NOT EXISTS (SELECT 1 FROM receipts rx WHERE rx.creator_key = {alias}.creator_key "
            f"AND rx.data_origin != 'live')")


def candidate_is_live(alias: str = "ca") -> str:
    return (f"(SELECT ru.data_origin FROM runs ru WHERE ru.run_id = {alias}.run_id) = 'live' AND "
            f"({alias}.creator_key IS NULL OR NOT EXISTS (SELECT 1 FROM receipts rx WHERE rx.creator_key = "
            f"{alias}.creator_key AND rx.data_origin != 'live'))")


def hidden_counts(db) -> dict[str, int]:
    """How many non-live records exist (shown as a note, never as data)."""
    return {
        "creators": db.scalar(f"SELECT COUNT(*) FROM creators c WHERE NOT ({creator_is_live('c')})") or 0,
        "runs": db.scalar("SELECT COUNT(*) FROM runs WHERE data_origin IS NULL OR data_origin != 'live'") or 0,
    }
