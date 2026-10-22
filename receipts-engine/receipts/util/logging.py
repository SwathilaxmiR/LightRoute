"""JSONL event logger: one JSON object per line."""
from __future__ import annotations

import hashlib
import json
import traceback
from pathlib import Path
from typing import Any

from receipts.util.dates import utcnow


class JsonlLogger:
    def __init__(self, path: str | Path | None):
        self.path = Path(path) if path else None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, event: str, **fields: Any) -> None:
        if not self.path:
            return
        rec = {"ts": utcnow().isoformat(), "event": event, **fields}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str, ensure_ascii=False) + "\n")

    def error(self, stage: str, cand_id: Any, exc: BaseException) -> None:
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        self.log("error", stage=stage, cand_id=cand_id, error=f"{type(exc).__name__}: {exc}",
                 traceback_hash=hashlib.sha256(tb.encode()).hexdigest()[:12])


NULL_LOGGER = JsonlLogger(None)
