"""Local operator console: `receipts ui` (http://127.0.0.1:8080).

Reads state.db live and shows REAL data only: creators whose every receipt came from a live fetch, and
candidates/runs from live runs. Demo, fixture and synthetic records are never displayed. Pages: dashboard (acceptance rules + readiness), leads, lead detail with every
receipt and its snapshot, rejects, runs/audits, seed test. Writes only through the engine's own functions
(manual checks, seed-test reply/sent logging). Third-party page content is only ever shown as escaped text.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import re
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, PackageLoader, select_autoescape
from markupsafe import Markup, escape

from receipts.cache import SnapshotStore
from receipts.db import DB, init_db
from receipts.guards import config_todos, yaml_todos
from receipts.models import CHANNEL_DISPLAY, PLATFORM_DISPLAY, SEGMENT_DISPLAY
from receipts.origin import candidate_is_live, creator_is_live, hidden_counts
from receipts.util.dates import utcnow

PAGE_SIZE = 50
CHECK_TITLES = [("real_relevant", "Real and relevant"), ("active", "Active"), ("commercial", "Commercial"),
                ("needs", "Needs IncuBrix"), ("reachable", "Reachable"), ("personalised", "Personalised"),
                ("complete_unique", "Complete and unique")]
KEYS = ["YOUTUBE_API_KEY", "PODCASTINDEX_KEY", "PODCASTINDEX_SECRET", "ANTHROPIC_API_KEY", "SMTP_USER", "IMAP_USER"]
MAX_SCREENSHOT_BYTES = 8 * 1024 * 1024


def _env() -> Environment:
    e = Environment(loader=PackageLoader("receipts.web", "templates"), autoescape=select_autoescape(["html"]))
    e.filters["pct"] = lambda v: f"{v:.0%}" if isinstance(v, (int, float)) else "-"
    e.filters["seg"] = lambda v: SEGMENT_DISPLAY.get(v, v or "-") if v else "-"
    e.filters["chan"] = lambda v: CHANNEL_DISPLAY.get(v, v or "-") if v else "-"
    e.filters["plat"] = lambda v: PLATFORM_DISPLAY.get(v, v or "-") if v else "-"
    e.filters["ts"] = lambda v: (v or "")[:16].replace("T", " ")
    return e


def highlight(text: str, quote: str) -> Markup:
    """Escape text; wrap the first case-insensitive, whitespace-tolerant match of quote in <mark>."""
    safe = str(escape(text))
    q = (quote or "").strip().split(" | manual note:")[0]
    if len(q) < 4:
        return Markup(safe)
    pat = r"\s+".join(re.escape(str(escape(w))) for w in q.split())
    m = re.search(pat, safe, re.I)
    if not m:
        return Markup(safe)
    return Markup(safe[:m.start()] + '<mark id="hit">' + safe[m.start():m.end()] + "</mark>" + safe[m.end():])


def create_app(settings) -> FastAPI:
    s = settings
    init_db(s.db_path).close()  # ensure schema / migrations once
    app = FastAPI(title="Receipts Engine console", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(packages=[("receipts.web", "static")]), name="static")
    env = _env()

    def db() -> DB:
        return DB(s.db_path)

    def render(name: str, **ctx) -> HTMLResponse:
        d = db()
        try:
            hidden = hidden_counts(d)
        finally:
            d.close()
        html = env.get_template(name).render(hidden=hidden, generated=utcnow().strftime("%Y-%m-%d %H:%M UTC"),
                                             db_path=str(s.db_path), **ctx)
        return HTMLResponse(html)

    # ------------------------------------------------------------------ dashboard
    @app.get("/", response_class=HTMLResponse)
    def dashboard():
        from receipts.stats import compute

        d = db()
        try:
            st = compute(d, s)
            runs = [dict(r) for r in d.fetch_all("SELECT * FROM runs WHERE data_origin='live' ORDER BY run_id DESC LIMIT 6")]
            candidates = d.scalar(f"SELECT COUNT(*) FROM candidates ca WHERE ca.platform != 'input' AND {candidate_is_live('ca')}") or 0
        finally:
            d.close()
        n = st.qualified or 1
        countries = [(k, v, v / n) for k, v in st.by_country.items()]
        segments = [(k, st.by_segment.get(k, 0), st.by_segment.get(k, 0) / n, t)
                    for k, t in s.quotas_final.segment_targets.items()]
        return render("dashboard.html", st=st, runs=runs, candidates=candidates, countries=countries, segments=segments,
                      readiness=readiness(s), max_share=s.quotas_final.max_country_share,
                      tolerance=s.quotas_final.segment_tolerance)

    # ------------------------------------------------------------------ leads
    @app.get("/leads", response_class=HTMLResponse)
    def leads(request: Request):
        p = request.query_params
        where, args = [creator_is_live("c")], []
        status = p.get("status", "Qualified")
        if status != "all":
            where.append("l.status = ?")
            args.append(status)
        for col, key in (("c.segment", "segment"), ("c.country_iso2", "country"), ("l.priority", "priority"),
                         ("ch.channel_type", "channel")):
            if p.get(key):
                where.append(f"{col} = ?")
                args.append(p[key])
        if p.get("q"):
            where.append("(c.display_name LIKE ? OR l.lead_id LIKE ? OR ch.handle_or_url LIKE ? OR c.website_domain LIKE ?)")
            args += [f"%{p['q']}%"] * 4
        order = {"score": "l.score DESC, l.lead_id", "lead": "l.lead_id", "name": "c.display_name COLLATE NOCASE",
                 "followers": "c.followers DESC"}.get(p.get("sort", "score"), "l.score DESC")
        page = max(1, int(p.get("page", "1") or 1))
        base = (" FROM leads l JOIN creators c USING(creator_key) "
                "LEFT JOIN channels ch ON ch.creator_key = c.creator_key AND ch.role = 'best' WHERE " + " AND ".join(where))
        d = db()
        try:
            total = d.scalar("SELECT COUNT(*)" + base, args) or 0
            rows = [dict(r) for r in d.fetch_all(
                "SELECT l.lead_id, l.score, l.priority, l.status, c.display_name, c.segment, c.country_iso2, "
                "c.primary_platform, c.followers, ch.channel_type AS best, ch.verified AS best_verified, "
                "(SELECT group_concat(sub_key, ', ') FROM receipts r WHERE r.creator_key = c.creator_key "
                " AND r.check_name = 'needs' AND r.sub_key != '' AND r.result = 'pass') AS needs" + base +
                f" ORDER BY {order} LIMIT ? OFFSET ?", [*args, PAGE_SIZE, (page - 1) * PAGE_SIZE])]
            facets = {
                "country": [r[0] for r in d.fetch_all("SELECT DISTINCT country_iso2 FROM creators c WHERE country_iso2 IS NOT NULL "
                                                       f"AND {creator_is_live('c')} ORDER BY 1")],
                "channel": [r[0] for r in d.fetch_all("SELECT DISTINCT channel_type FROM channels c WHERE role='best' "
                                                       f"AND {creator_is_live('c')} ORDER BY 1")],
            }
        finally:
            d.close()
        return render("leads.html", rows=rows, total=total, page=page, pages=max(1, -(-total // PAGE_SIZE)),
                      params=dict(p), status=status, facets=facets, segments=list(SEGMENT_DISPLAY))

    @app.get("/leads/{lead_id}", response_class=HTMLResponse)
    def lead_detail(lead_id: str):
        from receipts.leadrow import build_row
        from receipts.pipeline.s9_export import receipt_json

        d = db()
        try:
            lead = d.fetch_one(f"SELECT * FROM leads c WHERE lead_id=? AND {creator_is_live('c')}", (lead_id,))
            if lead is None:
                raise HTTPException(404, f"No real lead {lead_id} (demo, fixture and synthetic leads are not shown)")
            row = build_row(d, s, lead)
            rj = receipt_json(d, row, utcnow())
            key = lead["creator_key"]
            snaps = {r["path"]: r["snap_id"] for r in d.fetch_all(
                "SELECT s.path, s.snap_id FROM snapshots s JOIN receipts r ON r.snap_id = s.snap_id WHERE r.creator_key=?",
                (key,))}
            cards = []
            for name, title in CHECK_TITLES:
                v = rj["checks"].get(name)
                entries = v if isinstance(v, list) else ([v] if v else [])
                if name in ("commercial", "needs"):
                    agg = d.fetch_one("SELECT result, quote_or_metric, reason_code FROM receipts WHERE creator_key=? "
                                      "AND check_name=? AND sub_key=''", (key, name))
                    result = agg["result"] if agg else "missing"
                    summary = agg["quote_or_metric"] if agg else ""
                else:
                    result = entries[0]["result"] if entries else "missing"
                    summary = ""
                for e in entries:
                    e["snap_id"] = snaps.get(e.get("snapshot_path"))
                cards.append({"name": name, "title": title, "result": result, "summary": summary, "entries": entries})
            extra = [{"name": n, **(rj["checks"].get(n) or {})} for n in ("country", "language")]
            channels = [dict(r) for r in d.fetch_all(
                "SELECT * FROM channels WHERE creator_key=? ORDER BY CASE role WHEN 'best' THEN 0 WHEN 'backup' THEN 1 "
                "ELSE 2 END, channel_score DESC", (key,))]
            messages = []
            for m in d.fetch_all("SELECT * FROM messages WHERE creator_key=? ORDER BY channel, step", (key,)):
                m = dict(m)
                m["report"] = json.loads(m["validator_report"] or "[]")
                m["failed"] = [x for x in m["report"] if not x["pass"]]
                messages.append(m)
            items = [dict(r) for r in d.fetch_all(
                "SELECT * FROM content_items WHERE creator_key=? ORDER BY published_at DESC LIMIT 12", (key,))]
            identities = [dict(r) for r in d.fetch_all("SELECT * FROM identities WHERE creator_key=? ORDER BY platform", (key,))]
            breakdown = json.loads(lead["score_breakdown"])
            live = creator_is_live("c")
            prev = d.scalar(f"SELECT lead_id FROM leads c WHERE lead_id < ? AND {live} ORDER BY lead_id DESC LIMIT 1", (lead_id,))
            nxt = d.scalar(f"SELECT lead_id FROM leads c WHERE lead_id > ? AND {live} ORDER BY lead_id LIMIT 1", (lead_id,))
        finally:
            d.close()
        return render("lead.html", lead=dict(lead), row=row, cards=cards, extra=extra, channels=channels,
                      messages=messages, items=items, identities=identities, breakdown=breakdown,
                      weights=s.scoring.weights, prev=prev, nxt=nxt, today=utcnow().date().isoformat())

    # ------------------------------------------------------------------ snapshots
    @app.get("/snapshots/{snap_id}", response_class=HTMLResponse)
    def snapshot(snap_id: int, q: str = ""):
        d = db()
        try:
            row = d.fetch_one("SELECT * FROM snapshots WHERE snap_id=? AND data_origin='live'", (snap_id,))
            if row is None:
                raise HTTPException(404, f"No live snapshot {snap_id}")
            store = SnapshotStore(d, s.snapshots_dir)
            try:
                text = store.load_snapshot_text(snap_id)
            except OSError:
                text = "(snapshot file missing on disk)"
            used_by = [dict(r) for r in d.fetch_all(
                "SELECT r.check_name, r.sub_key, l.lead_id FROM receipts r LEFT JOIN leads l USING(creator_key) "
                "WHERE r.snap_id=? ORDER BY l.lead_id LIMIT 30", (snap_id,))]
        finally:
            d.close()
        found = bool(q) and "<mark" in str(highlight(text, q))
        return render("snapshot.html", snap=dict(row), body=highlight(text, q), q=q, found=found, used_by=used_by,
                      size=len(text))

    # ------------------------------------------------------------------ rejects / runs / seed test
    @app.get("/rejects", response_class=HTMLResponse)
    def rejects(request: Request):
        reason = request.query_params.get("reason", "")
        page = max(1, int(request.query_params.get("page", "1") or 1))
        d = db()
        try:
            live = candidate_is_live("ca")
            reasons = [dict(r) for r in d.fetch_all(f"SELECT reject_reason, COUNT(*) n FROM candidates ca WHERE "
                                                    f"ca.status='rejected' AND {live} GROUP BY reject_reason ORDER BY n DESC")]
            args: list = [reason] if reason else []
            cond = f"ca.status='rejected' AND {live}" + (" AND ca.reject_reason=?" if reason else "")
            total = d.scalar(f"SELECT COUNT(*) FROM candidates ca WHERE {cond}", args) or 0
            rows = [dict(r) for r in d.fetch_all(f"SELECT * FROM candidates ca WHERE {cond} ORDER BY cand_id DESC "
                                                 "LIMIT ? OFFSET ?", [*args, PAGE_SIZE, (page - 1) * PAGE_SIZE])]
        finally:
            d.close()
        return render("rejects.html", reasons=reasons, rows=rows, reason=reason, total=total, page=page,
                      pages=max(1, -(-total // PAGE_SIZE)))

    @app.get("/runs", response_class=HTMLResponse)
    def runs():
        d = db()
        try:
            rows = [dict(r) for r in d.fetch_all("SELECT * FROM runs WHERE data_origin='live' ORDER BY run_id DESC LIMIT 100")]
            from receipts.stats import balance_rules, compute

            st = compute(d, s)
            bal_rules = balance_rules(st, s)
        finally:
            d.close()
        for r in rows:
            try:
                r["reasons"] = sorted((json.loads(r["note"] or "{}").get("reasons") or {}).items(), key=lambda x: -x[1])[:3]
            except ValueError:
                r["reasons"] = []
        audits = []
        for p in sorted((s.out_dir / "logs").glob("audit_*.json"), reverse=True)[:10]:
            try:
                a = json.loads(p.read_text())
            except ValueError:
                continue
            if not a.get("fixture_mode"):  # audits of demo/fixture data are not shown
                audits.append(a)
        n = st.qualified or 1
        balance = {"qualified": st.qualified, "rules": bal_rules,
                   "countries": [(k, v / n) for k, v in st.by_country.items()],
                   "segments": [(k, st.by_segment.get(k, 0) / n, t) for k, t in s.quotas_final.segment_targets.items()]}
        return render("runs.html", rows=rows, audits=audits, balance=balance, tolerance=s.quotas_final.segment_tolerance)

    @app.get("/seedtest", response_class=HTMLResponse)
    def seedtest():
        from receipts.outreach import seedtest as st

        d = st.seed_db(s)
        try:
            sched = [dict(r) for r in d.fetch_all("SELECT * FROM schedule ORDER BY lead_id, channel, step")]
            replies = [dict(r) for r in d.fetch_all("SELECT * FROM replies ORDER BY reply_id DESC LIMIT 50")]
            clicks = d.scalar("SELECT COUNT(*) FROM clicks") or 0
            supp = [dict(r) for r in d.fetch_all("SELECT * FROM suppressions")]
        finally:
            d.close()
        return render("seedtest.html", sched=sched, replies=replies, clicks=clicks, supp=supp,
                      missing=st.prerequisites(s), leads=st.TEST_LEADS, channels=st.CHANNELS)

    # ------------------------------------------------------------------ actions (JSON)
    @app.post("/api/manual-check")
    async def api_manual_check(request: Request):
        from receipts.manual import ManualCheckError, record_manual_check

        body = await request.json()
        try:
            raw = base64.b64decode(body.get("screenshot_b64", ""), validate=True)
        except (binascii.Error, ValueError):
            return JSONResponse({"ok": False, "error": "screenshot is not valid base64"}, 400)
        if not raw or len(raw) > MAX_SCREENSHOT_BYTES:
            return JSONResponse({"ok": False, "error": "attach a screenshot under 8 MB"}, 400)
        suffix = Path(body.get("filename") or "shot.png").suffix.lower()
        if suffix not in (".png", ".jpg", ".jpeg", ".webp"):
            return JSONResponse({"ok": False, "error": "screenshot must be .png, .jpg or .webp"}, 400)
        tmp = s.out_dir / "manual_checks" / f"_upload{suffix}"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(raw)
        d = db()
        try:
            res = record_manual_check(d, s, body.get("lead_id", ""), body.get("channel", ""), body.get("active_date", ""),
                                      tmp, body.get("note", ""), utcnow())
        except (ManualCheckError, ValueError) as e:
            return JSONResponse({"ok": False, "error": str(e)}, 400)
        finally:
            d.close()
            tmp.unlink(missing_ok=True)
        return {"ok": True, **res}

    @app.post("/api/seed/log")
    async def api_seed_log(request: Request):
        from receipts.outreach import seedtest as st

        body = await request.json()
        lead, channel = body.get("lead_id", ""), body.get("channel", "")
        if lead not in st.TEST_LEADS or channel not in st.CHANNELS:
            return JSONResponse({"ok": False, "error": "unknown test lead or channel"}, 400)
        d = st.seed_db(s)
        try:
            if body.get("action") == "sent":
                ok = st.log_sent(d, lead, channel, int(body.get("step", 1)), utcnow())
                return {"ok": ok, "error": "" if ok else "no queued message for that step"}
            label = body.get("label", "")
            if label not in ("positive", "negative", "stop", "auto_reply", "other"):
                return JSONResponse({"ok": False, "error": "bad label"}, 400)
            st.record_reply(d, lead, channel, label, body.get("excerpt", "logged in console")[:300], "manual", utcnow())
            return {"ok": True}
        finally:
            d.close()

    @app.exception_handler(HTTPException)
    async def not_found(request: Request, exc: HTTPException):
        return HTMLResponse(env.get_template("error.html").render(message=exc.detail, hidden={}, generated="",
                                                                  db_path=str(s.db_path)), status_code=exc.status_code)

    return app


def readiness(s) -> list[dict]:
    """Everything the human must still provide (BUILD_SPEC section 16), without revealing secret values."""
    cfg = Path(s.config_dir)
    todo = config_todos(s, ["sender.name", "sender.role", "sender.postal_address", "sender.reply_to", "offer_line",
                            "project.user_agent"])
    items = [
        {"label": "Sender identity and offer line in settings.yaml", "ok": not todo, "detail": ", ".join(todo) or "filled"},
        {"label": "Seed-test mailboxes you own", "ok": bool([a for a in s.seed_test.allowed_recipients if "TODO" not in a]),
         "detail": "seed_test.allowed_recipients"},
    ]
    for k in KEYS:
        items.append({"label": f"{k} in .env", "ok": bool(os.environ.get(k)),
                      "detail": "optional" if k == "ANTHROPIC_API_KEY" else ("seed test" if k.endswith("_USER") else "")})
    items.append({"label": "Real replay fixtures recorded", "ok": Path("tests/fixtures/recorded_manifest.json").exists(),
                  "detail": "python scripts/record_fixtures.py"})
    wb = s.workbook.path
    items.append({"label": "Submission workbook in input/", "ok": bool(wb) and Path(wb).exists(), "detail": wb or "workbook.path"})
    for y in ("submission.yaml", "insight.yaml", "selfscore.yaml"):
        t = yaml_todos(cfg / y)
        items.append({"label": f"config/{y}", "ok": not t, "detail": f"{len(t)} TODO" if t else "filled"})
    items.append({"label": "launch_kit.md", "ok": Path("launch_kit.md").exists(), "detail": "from launch_kit_TEMPLATE.md"})
    return items
