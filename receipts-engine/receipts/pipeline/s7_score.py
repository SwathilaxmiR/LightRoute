"""s7: lead score (per creator) and final quota selection + priority (global)."""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field

from receipts.checks.c4_needs import strength
from receipts.db import DB
from receipts.outreach.hooks import parse_metric
from receipts.pipeline.runner import register_stage
from receipts.util.dates import parse_dt, utcnow

LEAD_SEQ = "lead_seq"


def fit_size(followers: int | None, s) -> float:
    t, f = s.thresholds, s.fit_size
    if followers is None:
        return f.unknown_value
    if t.fit_followers_min <= followers <= t.fit_followers_max:
        return 1.0
    if followers < t.fit_followers_min:
        return f.taper_floor + (1 - f.taper_floor) * followers / t.fit_followers_min
    if followers >= t.max_followers_hard:
        return f.taper_floor
    frac = (followers - t.fit_followers_max) / (t.max_followers_hard - t.fit_followers_max)
    return 1 - (1 - f.taper_floor) * frac


def components(db: DB, key: str, s, now) -> dict[str, float]:
    rec = db.fetch_all("SELECT * FROM receipts WHERE creator_key=?", (key,))
    needs = [r for r in rec if r["check_name"] == "needs" and r["sub_key"] and r["result"] == "pass"]
    strong = sum(1 for r in needs if strength(r["sub_key"], s) == "strong")
    medium = len(needs) - strong
    sc = s.scoring
    com = {r["sub_key"] for r in rec if r["check_name"] == "commercial" and r["sub_key"] and r["result"] == "pass"}
    act = parse_metric(next((r["quote_or_metric"] for r in rec if r["check_name"] == "active"), ""))
    c30, l60 = int(act.get("count_30d", 0) or 0), int(act.get("longform_60d", 0) or 0)
    best = db.fetch_one("SELECT * FROM channels WHERE creator_key=? AND role='best'", (key,))
    backup = db.fetch_one("SELECT * FROM channels WHERE creator_key=? AND role='backup'", (key,))
    reach = 0.6 * bool(best and best["verified"]) + 0.4 * bool(backup and backup["verified"])
    if best and best["verified"] and best["business_language"]:
        reach = 1.0
    followers = db.scalar("SELECT followers FROM creators WHERE creator_key=?", (key,))
    oldest = min((parse_dt(r["checked_at"]) for r in rec), default=now)
    age = max(0.0, (now - oldest).total_seconds() / 86400)
    return {
        "need": min(1.0, (strong * sc.need_strong_points + medium * sc.need_medium_points) / 30),
        "commercial": min(1.0, len(com) / 3),
        "activity": min(1.0, 0.5 * min(1, c30 / 16) + 0.5 * min(1, l60 / 6)),
        "reachability": reach,
        "fit_size": fit_size(followers, s),
        "freshness": 1 - min(1.0, age / s.freshness.max_evidence_age_days),
    }


def lead_score(comp: dict[str, float], s) -> float:
    w = s.scoring.weights
    return round(100 * sum(w[k] * comp[k] for k in w) / sum(w.values()), 1)


def next_lead_id(db: DB) -> str:
    n = int(db.meta_get(LEAD_SEQ, "0") or 0) + 1
    db.meta_set(LEAD_SEQ, str(n))
    return f"L{n:04d}"


def upsert_lead(db: DB, key: str, s, now) -> tuple[str, float]:
    comp = components(db, key, s, now)
    score = lead_score(comp, s)
    breakdown = json.dumps({"total": score, "components": {k: round(v, 3) for k, v in comp.items()},
                            "weights": s.scoring.weights})
    with db.transaction():
        row = db.fetch_one("SELECT lead_id FROM leads WHERE creator_key=?", (key,))
        if row:
            db.execute("UPDATE leads SET score=?, score_breakdown=?, status='Qualified' WHERE lead_id=?",
                       (score, breakdown, row["lead_id"]))
            return row["lead_id"], score
        lid = next_lead_id(db)
        db.insert("leads", {"lead_id": lid, "creator_key": key, "score": score, "score_breakdown": breakdown,
                            "priority": "C", "status": "Qualified"})
    return lid, score


@register_stage("s7")
async def score_stage(ctx, c, key: str, fr, origin: str) -> None:
    upsert_lead(ctx.db, key, ctx.settings, ctx.now)


# ------------------------------------------------------------------ final selection
@dataclass
class Balance:
    total_qualified: int = 0
    selected: int = 0
    by_country: dict[str, int] = field(default_factory=dict)
    by_segment: dict[str, int] = field(default_factory=dict)
    shares_country: dict[str, float] = field(default_factory=dict)
    shares_segment: dict[str, float] = field(default_factory=dict)
    violations: list[str] = field(default_factory=list)
    priority: dict[str, int] = field(default_factory=dict)


def _greedy(rows: list[dict], n: int, q) -> list[dict]:
    cap_c = math.floor(q.max_country_share * n)
    cap_s = {seg: math.floor((t + q.segment_tolerance) * n) for seg, t in q.segment_targets.items()}
    cc: dict[str, int] = {}
    sc: dict[str, int] = {}
    out = []
    for r in rows:
        if len(out) >= n:
            break
        if cc.get(r["country"], 0) >= cap_c or sc.get(r["segment"], 0) >= cap_s.get(r["segment"], n):
            continue
        out.append(r)
        cc[r["country"]] = cc.get(r["country"], 0) + 1
        sc[r["segment"]] = sc.get(r["segment"], 0) + 1
    return out


def _violations(sel: list[dict], q) -> list[str]:
    n = len(sel)
    if n == 0:
        return ["no qualified leads"]
    v = []
    cc: dict[str, int] = {}
    sc: dict[str, int] = {}
    for r in sel:
        cc[r["country"]] = cc.get(r["country"], 0) + 1
        sc[r["segment"]] = sc.get(r["segment"], 0) + 1
    for c, k in cc.items():
        if k / n > q.max_country_share + 1e-9:
            v.append(f"country {c} share {k / n:.0%} > {q.max_country_share:.0%}")
    counted = [c for c, k in cc.items() if k / n >= q.min_country_share_each]
    if len(counted) < q.min_countries:
        v.append(f"only {len(counted)} countries with >= {q.min_country_share_each:.0%} share (need {q.min_countries})")
    for seg, t in q.segment_targets.items():
        share = sc.get(seg, 0) / n
        if abs(share - t) > q.segment_tolerance + 1e-9:
            v.append(f"segment {seg} share {share:.0%} outside {t:.0%}±{q.segment_tolerance:.0%}")
    return v


def balanced_selection(rows: list[dict], q) -> tuple[list[dict], list[str]]:
    """Largest N whose greedy, score-ordered pick satisfies every quota rule."""
    rows = sorted(rows, key=lambda r: (-r["score"], r["lead_id"]))
    lo, hi, best = 1, len(rows), None
    while lo <= hi:  # binary search on N, then walk up a little (feasibility is near-monotone)
        mid = (lo + hi) // 2
        sel = _greedy(rows, mid, q)
        if len(sel) == mid and not _violations(sel, q):
            best, lo = sel, mid + 1
        else:
            hi = mid - 1
    if best is not None:
        n = len(best)
        for extra in range(n + 1, min(len(rows), n + 50) + 1):
            sel = _greedy(rows, extra, q)
            if len(sel) == extra and not _violations(sel, q):
                best = sel
        return best, []
    return rows, _violations(rows, q)


def a_eligible(db: DB, key: str, s) -> bool:
    best = db.fetch_one("SELECT * FROM channels WHERE creator_key=? AND role='best'", (key,))
    if not best or not best["verified"]:
        return False
    if best["channel_type"] in s.priority.require_manual_check_for and not best["manual_check_path"]:
        return False
    proofs = db.scalar("SELECT COUNT(*) FROM receipts WHERE creator_key=? AND check_name IN ('commercial','needs') "
                       "AND sub_key != '' AND result='pass' AND quote_or_metric IS NOT NULL", (key,)) or 0
    return proofs >= s.priority_split.min_proof_receipts


def messages_validated(db: DB, key: str, channel: str, steps: int = 3) -> bool:
    n = db.scalar("SELECT COUNT(*) FROM messages WHERE creator_key=? AND channel=? AND validated=1 AND step<=?",
                  (key, channel, steps)) or 0
    return n >= steps


def select_final(db: DB, s, write_path=None) -> Balance:
    from receipts.pipeline.s8_messages import message_channel

    q = s.quotas_final
    rows = [dict(r) for r in db.fetch_all(
        "SELECT l.lead_id, l.creator_key, l.score, c.country_iso2 AS country, c.segment FROM leads l "
        "JOIN creators c USING(creator_key) WHERE c.status='qualified' AND l.status IN ('Qualified','Reserve')")]
    bal = Balance(total_qualified=len(rows))
    sel, viol = balanced_selection(rows, q) if rows else ([], ["no qualified leads"])
    bal.violations = viol
    chosen = {r["lead_id"] for r in sel}
    # priority
    ranked = sorted(sel, key=lambda r: (-r["score"], r["lead_id"]))
    prio: dict[str, str] = {}
    a_count = 0
    for r in ranked:
        best = db.fetch_one("SELECT channel_type FROM channels WHERE creator_key=? AND role='best'", (r["creator_key"],))
        ch = message_channel(best["channel_type"]) if best else "email"
        if a_count < s.priority.target_count and a_eligible(db, r["creator_key"], s) and \
                messages_validated(db, r["creator_key"], ch):
            prio[r["lead_id"]] = "A"
            a_count += 1
    rest = [r for r in ranked if r["lead_id"] not in prio]
    n_b = math.ceil(s.priority_split.b_fraction * len(sel))
    for i, r in enumerate(rest):
        prio[r["lead_id"]] = "B" if i < n_b else "C"
    with db.transaction():
        for r in rows:
            if r["lead_id"] in chosen:
                db.execute("UPDATE leads SET status='Qualified', priority=? WHERE lead_id=?", (prio[r["lead_id"]], r["lead_id"]))
            else:
                db.execute("UPDATE leads SET status='Reserve', priority='C' WHERE lead_id=?", (r["lead_id"],))
    for r in sel:
        bal.by_country[r["country"]] = bal.by_country.get(r["country"], 0) + 1
        bal.by_segment[r["segment"]] = bal.by_segment.get(r["segment"], 0) + 1
    bal.selected = len(sel)
    if sel:
        bal.shares_country = {k: round(v / len(sel), 3) for k, v in sorted(bal.by_country.items())}
        bal.shares_segment = {k: round(v / len(sel), 3) for k, v in sorted(bal.by_segment.items())}
    for p in prio.values():
        bal.priority[p] = bal.priority.get(p, 0) + 1
    if write_path is not None:
        write_path.parent.mkdir(parents=True, exist_ok=True)
        write_path.write_text(json.dumps({"generated_at": utcnow().isoformat(), **asdict(bal)}, indent=2))
    return bal
