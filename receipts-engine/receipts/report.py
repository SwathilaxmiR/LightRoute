"""docs/run_report.md from the runs table."""
from __future__ import annotations

import json
from pathlib import Path

from receipts.util.dates import parse_dt, utcnow


def write_run_report(db, path: Path = Path("docs/run_report.md")) -> Path:
    runs = db.fetch_all("SELECT * FROM runs ORDER BY run_id")
    lines = ["# Run report", "", f"Generated {utcnow().strftime('%Y-%m-%d %H:%M UTC')} from the `runs` table.",
             "Bench rows are a synthetic replay, not live data.", "",
             "| Run | Kind | Started (UTC) | Minutes | Status | Input | Produced | Rejected | Dupes removed | Unchanged | "
             "Updated | YouTube units | LLM tokens in/out | LLM $ | Note |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in runs:
        a, b = parse_dt(r["started"]), parse_dt(r["ended"])
        mins = f"{(b - a).total_seconds() / 60:.1f}" if a and b else ""
        note = ""
        try:
            n = json.loads(r["note"] or "{}")
            top = sorted((n.get("reasons") or {}).items(), key=lambda x: -x[1])[:3]
            note = ", ".join(f"{k}={v}" for k, v in top)
        except ValueError:
            note = r["note"] or ""
        lines.append(f"| {r['run_id']} | {r['kind']} | {(r['started'] or '')[:16]} | {mins} | {r['status']} | "
                     f"{r['input_size']} | {r['produced']} | {r['rejected']} | {r['dupes_removed']} | {r['unchanged']} | "
                     f"{r['updated']} | {r['youtube_units']} | {r['llm_in_tokens']}/{r['llm_out_tokens']} | "
                     f"{(r['llm_cost_usd'] or 0):.4f} | {note} |")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    return path
