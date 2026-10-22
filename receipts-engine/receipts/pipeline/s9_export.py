"""s9: guarded export (BUILD_SPEC 9.3).

Order: G4 config -> re-render messages -> G5 freshness + live re-check -> quota balance -> G1 origin ->
c7 complete/unique -> write files. `demo=True` (keyless fixture demo) writes *_DEMO_not_live.csv files only,
never leads.csv, and uses a labelled sample sender so G4 does not block the demo.
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from receipts.checks.c7_complete_unique import complete_unique_result, duplicate_keys
from receipts.guards import (
    ConfigIncomplete,
    DataOriginViolation,
    StaleEvidence,
    assert_config_complete,
    assert_fresh,
    assert_live_origin,
)
from receipts.http import TRANSIENT, FixtureMissing
from receipts.leadrow import build_row
from receipts.models import LEAD_COLUMNS
from receipts.pipeline.common import save_receipt
from receipts.pipeline.s7_score import select_final
from receipts.pipeline.s8_messages import message_channel, rerender_all
from receipts.sources.rss import parse_feed
from receipts.util.dates import parse_dt, utcnow, within_days

REJECT_COLUMNS = ["candidate_ref", "platform", "source_id", "url", "stage", "reject_reason", "detail", "run_id",
                  "rejected_at"]
CRM_COLUMNS = ["lead_id", "creator_name", "segment", "country", "website", "primary_platform", "profile_url",
               "contact_channel", "contact_value", "backup_channel", "backup_value", "priority", "score", "hook",
               "message_1", "message_2", "message_3", "sequence_name", "next_step_due", "owner", "status",
               "source_basis", "opt_out_status", "created_at"]
HUMAN_YAMLS = ["submission.yaml", "insight.yaml", "selfscore.yaml"]


class ExportBlocked(Exception):
    pass


@dataclass
class ExportResult:
    written: int = 0
    demo: bool = False
    dropped: dict[str, int] = field(default_factory=dict)
    balance: dict = field(default_factory=dict)
    paths: dict[str, str] = field(default_factory=dict)
    rerender: dict[str, int] = field(default_factory=dict)


def demo_settings(s):
    d = s.model_copy(deep=True)
    if "TODO" in d.sender.name:
        d.sender.name = "Demo Sender"
    if "TODO" in d.sender.postal_address:
        d.sender.postal_address = "1 Demo Street, Demo Town"
    if "TODO" in d.offer_line:
        d.offer_line = "Demo offer line: replace offer_line in settings.yaml after using the product."
    return d


def _drop(db, lead_id: str, key: str, reason: str, detail: str, res: ExportResult, run_id=None) -> None:
    now = utcnow().isoformat()
    with db.transaction():
        db.execute("UPDATE leads SET status='Rejected' WHERE lead_id=?", (lead_id,))
        db.execute("UPDATE creators SET status='rejected', reject_reason=? WHERE creator_key=?", (reason, key))
        db.execute("UPDATE candidates SET status='rejected', stage='exported', reject_reason=?, reject_detail=?, "
                   "rejected_at=?, run_id=COALESCE(?, run_id) WHERE creator_key=? AND status='done' "
                   "AND reject_reason IS NULL", (reason, detail[:300], now, run_id, key))
    res.dropped[reason] = res.dropped.get(reason, 0) + 1


async def recheck(ctx, lead, s, now) -> str | None:
    """Cheap re-check: latest upload of the primary platform still recent; best channel source page still 200.
    Returns a failure detail or None."""
    db = ctx.db
    key = lead["creator_key"]
    checked = [r["checked_at"] for r in db.fetch_all("SELECT checked_at FROM receipts WHERE creator_key=?", (key,))]
    try:
        assert_fresh(checked, s.freshness.max_evidence_age_days, now, lead["lead_id"])  # G5
    except StaleEvidence as e:
        return str(e)
    cr = db.fetch_one("SELECT * FROM creators WHERE creator_key=?", (key,))
    window = s.thresholds.window_days_long
    try:
        if cr["primary_platform"] == "youtube":
            ups = await ctx.yt.uploads("UU" + cr["primary_id"][2:], max_items=5)
            dates = [parse_dt(u.published_at) for u in ups if u.published_at]
            if not dates or not within_days(max(dates), window, now):
                return "no upload in the activity window on re-check"
        elif cr["primary_platform"] == "podcast":
            feed = db.scalar("SELECT url FROM identities WHERE creator_key=? AND platform='rss'", (key,))
            parsed = await parse_feed(ctx.http, feed) if feed else None
            if not parsed or not parsed[1] or not within_days(max(i.published_at for i in parsed[1]), window, now):
                return "feed has no recent episode on re-check"
        best = db.fetch_one("SELECT * FROM channels WHERE creator_key=? AND role='best'", (key,))
        if best and best["channel_type"] not in ("linkedin", "instagram", "x"):
            kind = "api" if "youtube.com" in best["source_url"] else "web"
            if kind == "web":
                r = await ctx.http.get(best["source_url"], kind="web", cache=False)
                if not r.ok:
                    return f"best channel source {best['source_url']} returned {r.status_code}"
    except FixtureMissing:
        return None if ctx.http.fixture_mode else "re-check response missing"
    except TRANSIENT as e:
        return f"re-check failed: {type(e).__name__}"
    except Exception as e:  # robots now disallows, YouTube error, parse failure
        return f"re-check failed: {type(e).__name__}: {e}"
    return None


async def export_all(ctx, *, workbook: bool = False, allow_imbalance: bool = False, demo: bool = False) -> ExportResult:
    s = demo_settings(ctx.settings) if demo else ctx.settings
    db, out = ctx.db, ctx.settings.out_dir
    res = ExportResult(demo=demo)
    now = utcnow()

    # 1. G4
    extra = [Path(s.config_dir) / y for y in HUMAN_YAMLS] if workbook else []
    assert_config_complete(s, extra)

    # 2. messages with current settings
    res.rerender = rerender_all(db, ctx.store, s, now, None)

    # 3. G5 + cheap live re-check
    leads = db.fetch_all("SELECT l.* FROM leads l JOIN creators c USING(creator_key) "
                         "WHERE c.status='qualified' AND l.status IN ('Qualified','Reserve') ORDER BY l.lead_id")
    if ctx.settings.freshness.final_recheck:
        for lead in leads:
            fail = await recheck(ctx, lead, s, now)
            if fail:
                _drop(db, lead["lead_id"], lead["creator_key"], "stale_evidence", fail, res, ctx.run_id)

    # 4. balance
    bal = select_final(db, s, out / "logs" / "balance.json")
    res.balance = bal.__dict__
    if bal.violations and not allow_imbalance:
        raise ExportBlocked("quota balance violated: " + "; ".join(bal.violations) +
                            " (see out/logs/balance.json; --allow-imbalance is for drafts only)")

    # 5. rows: G1, messages validated, completeness/uniqueness
    selected = db.fetch_all("SELECT * FROM leads WHERE status='Qualified' ORDER BY lead_id")
    rows: list[dict] = []
    for lead in selected:
        key = lead["creator_key"]
        rec = [dict(r) for r in db.fetch_all("SELECT data_origin FROM receipts WHERE creator_key=?", (key,))]
        if not demo:
            try:
                assert_live_origin(rec, lead["lead_id"])  # G1
            except DataOriginViolation as e:
                res.dropped["origin_not_live"] = res.dropped.get("origin_not_live", 0) + 1
                ctx.log.log("export_refused", lead_id=lead["lead_id"], reason=str(e))
                continue
        best = db.fetch_one("SELECT channel_type FROM channels WHERE creator_key=? AND role='best'", (key,))
        ch = message_channel(best["channel_type"]) if best else "email"
        bad_msgs = db.scalar("SELECT COUNT(*) FROM messages WHERE creator_key=? AND validated=0", (key,))
        has_first = db.scalar("SELECT COUNT(*) FROM messages WHERE creator_key=? AND channel=? AND step=1 AND validated=1",
                              (key, ch))
        if bad_msgs or not has_first:
            # excluded from this export only: a copy/config problem must not reject a qualified creator
            res.dropped["message_validation_failed"] = res.dropped.get("message_validation_failed", 0) + 1
            ctx.log.log("export_skipped", lead_id=lead["lead_id"], reason="message_validation_failed", invalid=bad_msgs)
            continue
        rows.append(build_row(db, s, lead))
    dups = duplicate_keys(rows)
    final: list[dict] = []
    for r in rows:
        cu = complete_unique_result(r, r["lead_id"] in dups, r["creator_key"], r["checked_date"])
        with db.transaction():
            save_receipt(db, r["creator_key"], cu, "live")
        if not cu.passed:
            _drop(db, r["lead_id"], r["creator_key"], cu.reason_code, cu.quote_or_metric or "", res)
            continue
        final.append(r)

    # 6. write
    out.mkdir(parents=True, exist_ok=True)
    suffix = "_DEMO_not_live" if demo else ""
    res.paths["leads"] = str(write_leads(out / f"leads{suffix}.csv", final))
    res.paths["rejects"] = str(write_rejects(db, out / "rejects.csv"))
    res.paths["crm"] = str(write_crm(db, s, out / f"crm_export{suffix}.csv", final, now))
    res.paths["receipts"] = str(write_receipt_files(db, out / "receipts", final, now))
    from receipts.outreach.folder import write_outreach_folder
    from receipts.viewer import build_viewer

    res.paths["outreach"] = str(write_outreach_folder(db, s, out / "outreach", final))
    res.paths["viewer"] = str(build_viewer(db, s, out / "viewer", final, demo=demo))
    if workbook and s.workbook.path:
        from receipts.workbook import fill_workbook

        res.paths["workbook"] = str(fill_workbook(db, s, Path(s.workbook.path), out / "workbook_fill.xlsx",
                                                  out / f"leads{suffix}.csv"))
    with db.transaction():
        for r in final:
            db.execute("UPDATE leads SET first_exported_at=COALESCE(first_exported_at, ?), last_exported_at=? "
                       "WHERE lead_id=?", (now.isoformat(), now.isoformat(), r["lead_id"]))
    res.written = len(final)
    ctx.log.log("export", written=res.written, dropped=res.dropped, demo=demo)
    return res


def write_leads(path: Path, rows: list[dict]) -> Path:
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
        w.writerow(LEAD_COLUMNS)
        for r in sorted(rows, key=lambda r: r["lead_id"]):
            w.writerow([r[c] for c in LEAD_COLUMNS])
    return path


def write_rejects(db, path: Path) -> Path:
    rows = db.fetch_all("SELECT * FROM candidates WHERE status='rejected' ORDER BY cand_id")
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(REJECT_COLUMNS)
        for r in rows:
            w.writerow([f"C{r['cand_id']:05d}", r["platform"], r["source_id"], r["url"], r["stage"], r["reject_reason"],
                        r["reject_detail"] or "", r["run_id"] or "", r["rejected_at"] or ""])
    return path


def write_crm(db, s, path: Path, rows: list[dict], now) -> Path:
    due = (now + timedelta(days=s.follow_up_days[0])).strftime("%Y-%m-%d")
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(CRM_COLUMNS)
        for r in rows:
            ch = message_channel(r["best_type"])
            msgs = {m["step"]: m for m in db.fetch_all("SELECT * FROM messages WHERE creator_key=? AND channel=?",
                                                       (r["creator_key"], ch))}
            body = [_full_text(msgs.get(step)) for step in (1, 2, 3)]
            seg = (db.scalar("SELECT segment FROM creators WHERE creator_key=?", (r["creator_key"],)) or "")
            w.writerow([r["lead_id"], r["creator_name"], r["creator_segment"], r["country_iso2"], r["website_domain"],
                        r["primary_platform"], r["profile_url"], r["best_channel"], r["channel_handle_or_url"],
                        r["backup_channel"], r["backup_value"], r["priority"], r["lead_score"],
                        r["personalization_hook"], *body, f"{seg}/{ch}",
                        due, "", "New", f"public business contact, source: {r['channel_evidence_url']}", "none",
                        now.strftime("%Y-%m-%d")])
    return path


def _full_text(m) -> str:
    if not m:
        return ""
    return (f"Subject: {m['subject']}\n\n" if m["subject"] else "") + m["body"]


def _receipt_obj(db, r) -> dict:
    snap = db.fetch_one("SELECT path FROM snapshots WHERE snap_id=?", (r["snap_id"],)) if r["snap_id"] else None
    return {"sub_key": r["sub_key"], "result": r["result"], "evidence_url": r["evidence_url"],
            "quote_or_metric": r["quote_or_metric"], "snapshot_path": snap["path"] if snap else None,
            "checked_at": r["checked_at"], "verified_by": r["verified_by"], "reason_code": r["reason_code"],
            "data_origin": r["data_origin"]}


def receipt_json(db, row: dict, now) -> dict:
    key = row["creator_key"]
    rec = db.fetch_all("SELECT * FROM receipts WHERE creator_key=? ORDER BY check_name, sub_key", (key,))

    def one(name):
        r = next((x for x in rec if x["check_name"] == name and x["sub_key"] == ""), None)
        return _receipt_obj(db, r) if r else None

    lead = db.fetch_one("SELECT * FROM leads WHERE lead_id=?", (row["lead_id"],))
    bd = json.loads(lead["score_breakdown"])
    chans = {}
    for role in ("best", "backup"):
        c = db.fetch_one("SELECT * FROM channels WHERE creator_key=? AND role=?", (key, role))
        if c:
            chans[role] = {k: c[k] for k in ("channel_type", "handle_or_url", "source_url", "source_quote",
                                              "business_language", "live_ok", "name_match", "mx_ok", "last_active",
                                              "active_proof", "manual_check_path", "channel_score", "verified",
                                              "assisted_send", "checked_at")}
    return {
        "lead_id": row["lead_id"], "creator_key": key, "creator_name": row["creator_name"],
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        "checks": {
            "real_relevant": one("real_relevant"), "active": one("active"),
            "commercial": [_receipt_obj(db, r) for r in rec if r["check_name"] == "commercial" and r["sub_key"]],
            "needs": [_receipt_obj(db, r) for r in rec if r["check_name"] == "needs" and r["sub_key"]],
            "reachable": one("reachable"), "personalised": one("personalised"), "complete_unique": one("complete_unique"),
            "country": one("country"), "language": one("language"),
        },
        "score": {"total": bd["total"], "components": bd["components"]},
        "channels": chans,
    }


def write_receipt_files(db, d: Path, rows: list[dict], now) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    for r in rows:
        (d / f"{r['lead_id']}.json").write_text(json.dumps(receipt_json(db, r, now), indent=2, ensure_ascii=False),
                                                 encoding="utf-8")
    return d


__all__ = ["export_all", "ExportBlocked", "ConfigIncomplete"]
