"""Shared check helpers. CheckResult lives in receipts.models."""
from __future__ import annotations

from receipts.models import CheckResult

__all__ = ["CheckResult", "metric", "passed", "failed", "na"]


def metric(**kv) -> str:
    """'k=v; k=v' metric string, stable order as given."""
    parts = []
    for k, v in kv.items():
        if isinstance(v, (list, tuple)):
            v = ",".join(str(x) for x in v)
        parts.append(f"{k}={v}")
    return "; ".join(parts)


def passed(check: str, sub_key: str = "", **kw) -> CheckResult:
    return CheckResult(check_name=check, sub_key=sub_key, result="pass", **kw)


def failed(check: str, reason: str, sub_key: str = "", **kw) -> CheckResult:
    return CheckResult(check_name=check, sub_key=sub_key, result="fail", reason_code=reason, **kw)


def na(check: str, reason: str, sub_key: str = "", **kw) -> CheckResult:
    return CheckResult(check_name=check, sub_key=sub_key, result="na", reason_code=reason, **kw)
