"""`receipts trace LEAD_ID`: every receipt behind a lead, with URL, fetch time, quote/metric and snapshot."""
from __future__ import annotations

import json

from rich.console import Console

from receipts.leadrow import build_row
from receipts.pipeline.s9_export import receipt_json
from receipts.util.dates import utcnow

ORDER = [("real_relevant", "real_relevant"), ("active", "active"), ("commercial", "commercial"), ("needs", "needs"),
         ("reachable", "reachable"), ("personalised", "personalised"), ("complete_unique", "complete+unique")]


class TraceError(Exception):
    pass


def trace_data(db, s, lead_id: str) -> dict:
    lead = db.fetch_one("SELECT * FROM leads WHERE lead_id=?", (lead_id,))
    if lead is None:
        raise TraceError(f"no lead {lead_id}")
    row = build_row(db, s, lead)
    data = receipt_json(db, row, utcnow())
    data["row"] = {k: row[k] for k in ("creator_segment", "country_iso2", "lead_score", "priority", "status")
                   if k in row} | {"status": lead["status"], "priority": lead["priority"]}
    data["messages"] = [dict(m) for m in db.fetch_all("SELECT channel, step, subject, body, validated FROM messages "
                                                      "WHERE creator_key=? ORDER BY channel, step", (lead["creator_key"],))]
    data["snapshot_fetched"] = {
        r["snap_id"]: r["fetched_at"] for r in db.fetch_all(
            "SELECT s.snap_id, s.fetched_at FROM snapshots s JOIN receipts r ON r.snap_id=s.snap_id WHERE r.creator_key=?",
            (lead["creator_key"],))}
    return data


def _fmt(r: dict | None) -> str:
    if not r:
        return "(missing)"
    parts = []
    if r.get("evidence_url"):
        parts.append(r["evidence_url"])
    if r.get("checked_at"):
        parts.append(f"checked {r['checked_at'][:16]}Z")
    q = (r.get("quote_or_metric") or "").replace("\n", " ")
    if q:
        parts.append(f'"{q[:160]}"')
    if r.get("snapshot_path"):
        parts.append(f"snap {r['snapshot_path']}")
    if r.get("verified_by"):
        parts.append(r["verified_by"])
    if r.get("data_origin") and r["data_origin"] != "live":
        parts.append(f"[{r['data_origin'].upper()}]")
    return "  ".join(parts)


def print_trace(db, s, lead_id: str, as_json: bool = False, console: Console | None = None) -> None:
    con = console or Console()
    d = trace_data(db, s, lead_id)
    if as_json:
        con.print_json(json.dumps(d, default=str))
        return
    row = d["row"]
    con.print(f"[bold]LEAD {d['lead_id']}[/bold]  {d['creator_name']}  ({row['creator_segment']}, {row['country_iso2']}, "
              f"score {d['score']['total']}, Priority {row['priority']}, {row['status']})  {d['creator_key']}")
    for i, (key, label) in enumerate(ORDER, 1):
        v = d["checks"].get(key)
        if isinstance(v, list):
            ok = [x for x in v if x["result"] == "pass"]
            res = "PASS" if ok else "FAIL"
            con.print(f"{i} {label:<16}[{'green' if ok else 'red'}]{res}[/]  " +
                      ", ".join(x["sub_key"] for x in ok))
            for x in ok:
                con.print(f"    - {x['sub_key']}: {_fmt(x)}")
        else:
            res = (v or {}).get("result", "missing").upper()
            con.print(f"{i} {label:<16}[{'green' if res == 'PASS' else 'red'}]{res}[/]  {_fmt(v)}")
    for extra in ("country", "language"):
        v = d["checks"].get(extra)
        con.print(f"  {extra:<17}{(v or {}).get('result', '-').upper()}  {_fmt(v)}")
    ch = d["channels"]
    for role in ("best", "backup"):
        if role in ch:
            c = ch[role]
            con.print(f"  {role:<17}{c['channel_type']} {c['handle_or_url']}  source: {c['source_url']}  "
                      f"\"{c['source_quote'][:100]}\"  verified={c['verified']} mx={c['mx_ok']}")
    con.print("  score            " + ", ".join(f"{k}={v}" for k, v in d["score"]["components"].items()))
    for m in d["messages"]:
        con.print(f"  message {m['channel']} step {m['step']} validated={m['validated']}")
