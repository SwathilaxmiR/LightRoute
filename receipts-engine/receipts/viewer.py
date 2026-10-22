"""Static HTML viewer (out/viewer/): index, one page per lead, summary. No external assets."""
from __future__ import annotations

import json
import os
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from receipts.util.dates import utcnow

TEMPLATES = Path(__file__).resolve().parent.parent / "viewer_templates"
CARD_TITLES = [("real_relevant", "Real and relevant"), ("active", "Active"), ("commercial", "Commercial"),
               ("needs", "Needs IncuBrix"), ("reachable", "Reachable"), ("personalised", "Personalised"),
               ("complete_unique", "Complete and unique")]


def _env() -> Environment:
    return Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=select_autoescape(["html", "j2"]))


def build_viewer(db, s, d: Path, rows: list[dict], demo: bool = False) -> Path:
    from receipts.pipeline.s9_export import receipt_json

    d.mkdir(parents=True, exist_ok=True)
    (d / "lead").mkdir(exist_ok=True)
    env = _env()
    now = utcnow()
    gen = now.strftime("%Y-%m-%d %H:%M UTC")
    rows = sorted(rows, key=lambda r: r["lead_id"])
    common = {"demo": demo, "generated": gen}
    (d / "index.html").write_text(env.get_template("index.html.j2").render(
        rows=rows, root="", segments=sorted({r["creator_segment"] for r in rows}),
        countries=sorted({r["country_iso2"] for r in rows}), **common), encoding="utf-8")
    for r in rows:
        rj = receipt_json(db, r, now)
        cards = []
        for name, title in CARD_TITLES:
            v = rj["checks"].get(name)
            items = v if isinstance(v, list) else ([v] if v else [])
            for it in items:
                if it.get("snapshot_path"):
                    it["snapshot_rel"] = os.path.relpath(Path(it["snapshot_path"]).resolve(), (d / "lead").resolve())
            if name in ("commercial", "needs"):
                items = [i for i in items if i["result"] == "pass"]
                agg = db.fetch_one("SELECT result FROM receipts WHERE creator_key=? AND check_name=? AND sub_key=''",
                                   (r["creator_key"], name))
                result = agg["result"] if agg else "fail"
            else:
                result = items[0]["result"] if items else "fail"
            cards.append({"title": title, "result": result, "entries": items})
        msgs = [dict(m) for m in db.fetch_all("SELECT * FROM messages WHERE creator_key=? ORDER BY channel, step",
                                              (r["creator_key"],))]
        (d / "lead" / f"{r['lead_id']}.html").write_text(env.get_template("lead.html.j2").render(
            row=r, cards=cards, score=rj["score"], weights=s.scoring.weights, channels=rj["channels"], messages=msgs,
            root="../", **common), encoding="utf-8")
    by_seg: dict[str, int] = {}
    by_cty: dict[str, int] = {}
    mix: dict[str, int] = {}
    prio: dict[str, int] = {}
    for r in rows:
        by_seg[r["creator_segment"]] = by_seg.get(r["creator_segment"], 0) + 1
        by_cty[r["country_iso2"]] = by_cty.get(r["country_iso2"], 0) + 1
        mix[r["best_channel"]] = mix.get(r["best_channel"], 0) + 1
        prio[r["priority"]] = prio.get(r["priority"], 0) + 1
    bal_path = s.out_dir / "logs" / "balance.json"
    balance = json.loads(bal_path.read_text()) if bal_path.exists() else {"violations": []}
    audits = sorted((s.out_dir / "logs").glob("audit_*.json"))
    audit = json.loads(audits[-1].read_text()) if audits else None
    reasons = db.fetch_all("SELECT reject_reason, COUNT(*) n FROM candidates WHERE status='rejected' "
                           "GROUP BY reject_reason ORDER BY n DESC LIMIT 12")
    runs = [dict(x) for x in db.fetch_all("SELECT * FROM runs ORDER BY run_id DESC LIMIT 30")]
    (d / "summary.html").write_text(env.get_template("summary.html.j2").render(
        rows=rows, by_segment=dict(sorted(by_seg.items())), by_country=dict(sorted(by_cty.items(), key=lambda x: -x[1])),
        channel_mix=dict(sorted(mix.items(), key=lambda x: -x[1])), prio=prio, balance=balance, audit=audit,
        reasons=[(x["reject_reason"], x["n"]) for x in reasons],
        rejected=db.scalar("SELECT COUNT(*) FROM candidates WHERE status='rejected'"), runs=runs, root="", **common),
        encoding="utf-8")
    return d
