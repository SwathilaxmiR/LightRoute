"""Check c7: complete and unique. Same completeness rule as the workbook (required cells non-empty,
plus backup channel, counts and score) and uniqueness by canonical key, stable id and profile URL."""
from __future__ import annotations

from receipts.checks.base import CheckResult, failed, passed
from receipts.models import LEAD_COLUMNS, LEAD_OPTIONAL

COMPUTED = {"duplicate_check", "completeness_check"}


def missing_fields(row: dict) -> list[str]:
    return [c for c in LEAD_COLUMNS if c not in LEAD_OPTIONAL | COMPUTED and str(row.get(c, "")).strip() == ""]


def duplicate_keys(rows: list[dict]) -> set[str]:
    """lead_ids whose stable id, profile URL or creator key repeats an earlier row."""
    seen: dict[str, set[str]] = {"stable_creator_id": set(), "profile_url": set(), "creator_key": set()}
    dup = set()
    for r in rows:
        for k, bucket in seen.items():
            v = str(r.get(k, "")).strip().casefold()
            if v and v in bucket:
                dup.add(r["lead_id"])
            bucket.add(v)
    return dup


def complete_unique_result(row: dict, is_dup: bool, creator_key: str, checked_at: str) -> CheckResult:
    miss = missing_fields(row)
    req = len(LEAD_COLUMNS) - len(LEAD_OPTIONAL | COMPUTED)
    m = (f"required_fields={req - len(miss)}/{req}; duplicate_check={'DUPLICATE' if is_dup else 'UNIQUE'}; "
         f"creator_key={creator_key}; checked_at={checked_at}")
    if miss:
        return failed("complete_unique", "incomplete", quote_or_metric=m + f"; missing={','.join(miss)}")
    if is_dup:
        return failed("complete_unique", "duplicate_row", quote_or_metric=m)
    return passed("complete_unique", quote_or_metric=m, evidence_url=row.get("profile_url"))
