"""Check c6: personalised. The hook names a real recent item (exact title present in its snapshot)
and every message for the lead passed all validators."""
from __future__ import annotations

from receipts.checks.base import CheckResult, failed, passed


def personalised_result(hook: str, hook_url: str, snap_id: int | None, reports: list[list[dict]],
                        config_todo: bool) -> CheckResult:
    total = sum(len(r) for r in reports)
    ok = sum(1 for r in reports for x in r if x["pass"])
    m = f"hook={hook} | validators={ok}/{total} across {len(reports)} message(s)"
    if reports and ok == total:
        return passed("personalised", evidence_url=hook_url, snap_id=snap_id, quote_or_metric=m,
                      details={"messages": len(reports)})
    return failed("personalised", "config_todo" if config_todo else "message_validation_failed",
                  evidence_url=hook_url, snap_id=snap_id, quote_or_metric=m)
