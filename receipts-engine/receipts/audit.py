"""`receipts audit`: seeded random self-audit (BUILD_SPEC 11.2).

Default: re-verify against stored snapshots (quotes verbatim, metrics recomputed from content_items).
--live: also re-fetch every evidence URL (HTTP 200) and re-verify quotes on the fresh page; re-resolve MX.
A lead passes only if all 7 checks pass.
"""
from __future__ import annotations

import json
import random
import re
import statistics
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, time
from pathlib import Path

from receipts.checks.c2_active import compute_activity
from receipts.checks.c5_reachable import name_match_handle, name_tokens
from receipts.checks.c7_complete_unique import missing_fields
from receipts.guards import GuardViolation, assert_contact_provenance
from receipts.http import TRANSIENT, FixtureMissing
from receipts.leadrow import build_row
from receipts.models import Candidate, CollectedCreator, ContentItem
from receipts.origin import creator_is_live
from receipts.outreach.hooks import parse_metric
from receipts.sources.dns import mx_hosts
from receipts.util.dates import parse_dt, utcnow, within_days
from receipts.util.text import normalise, verify_quote

CHECKS = ["real_relevant", "active", "commercial", "needs", "reachable", "personalised", "complete_unique"]
METRIC_RE = re.compile(r"^[a-z_A-Z0-9]+=")


@dataclass
class LeadAudit:
    lead_id: str
    creator_key: str
    checks: dict[str, dict] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return all(c["ok"] for c in self.checks.values())


def _items(db, key) -> list[ContentItem]:
    out = []
    for r in db.fetch_all("SELECT * FROM content_items WHERE creator_key=?", (key,)):
        out.append(ContentItem(platform=r["platform"], item_id=r["item_id"], url=r["url"], title=r["title"],
                               published_at=parse_dt(r["published_at"]), duration_s=r["duration_s"],
                               is_short=bool(r["is_short"]), has_captions=None if r["has_captions"] is None else bool(r["has_captions"]),
                               paid_placement=None if r["paid_placement"] is None else bool(r["paid_placement"]),
                               description=r["description"], snap_id=r["snap_id"]))
    return out


def _close(a: float, b: float, tol: float) -> bool:
    return abs(a - b) <= max(1.0, tol * max(abs(a), abs(b)))


class Auditor:
    def __init__(self, ctx, live: bool):
        self.ctx, self.db, self.s, self.live = ctx, ctx.db, ctx.settings, live
        self.fetched: dict[str, tuple[int, str]] = {}

    # ---------------------------------------------------------------- evidence helpers
    def stored_text(self, snap_id) -> str:
        return self.ctx.store.load_snapshot_text(snap_id) if snap_id else ""

    async def live_text(self, url: str) -> tuple[int, str]:
        if url in self.fetched:
            return self.fetched[url]
        try:
            kind = "api" if "youtube.com/" in url or "podcastindex.org/" in url else "web"
            r = await self.ctx.http.get(url, kind=kind, cache=False)
            res = (r.status_code, self.ctx.store.load_snapshot_text(r.snap_id) if r.snap_id else r.text)
        except FixtureMissing:
            res = (0, "")
        except TRANSIENT as e:
            res = (0, f"error {type(e).__name__}")
        except GuardViolation as e:
            res = (0, f"refused {e}")
        self.fetched[url] = res
        return res

    async def verify_receipt(self, r) -> tuple[bool, str]:
        """Quote receipts: quote verbatim in the snapshot (and in a fresh fetch when live)."""
        q = r["quote_or_metric"] or ""
        if not r["snap_id"]:
            return False, "no snapshot"
        if not verify_quote(q, self.stored_text(r["snap_id"]), self.s.text.quote_min_chars):
            return False, "quote not in stored snapshot"
        if self.live and r["evidence_url"] and not r["evidence_url"].startswith("https://www.youtube.com"):
            code, text = await self.live_text(r["evidence_url"])
            if code != 200:
                return False, f"evidence URL status {code}"
            if not verify_quote(q, text, self.s.text.quote_min_chars):
                return False, "quote no longer on live page"
        return True, ""

    # ---------------------------------------------------------------- per check
    async def real_relevant(self, key, rec, items) -> tuple[bool, str]:
        r = rec.get(("real_relevant", ""))
        if not r or r["result"] != "pass":
            return False, "receipt missing or failing"
        q = r["quote_or_metric"] or ""
        if not METRIC_RE.match(q):
            return await self.verify_receipt(r)
        m = parse_metric(q)
        if "episodes" in m:
            pod = [i for i in items if i.platform == "podcast" and i.duration_s]
            med = statistics.median([i.duration_s for i in pod]) / 60 if pod else 0
            ok = len(pod) >= self.s.thresholds.podcast_min_episodes and med >= self.s.thresholds.podcast_min_median_minutes
            return ok, "" if ok else f"recomputed episodes={len(pod)} median={med:.1f}"
        if "longform_60d" in m:
            return int(m["longform_60d"]) >= self.s.thresholds.active_longform_60d, ""
        return True, ""

    async def active(self, key, rec, items) -> tuple[bool, str]:
        r = rec.get(("active", ""))
        if not r or r["result"] != "pass":
            return False, "receipt missing or failing"
        m = parse_metric(r["quote_or_metric"])
        end = datetime.combine(datetime.fromisoformat(m["window_end"]).date(), time(23, 59), tzinfo=parse_dt(r["checked_at"]).tzinfo)
        a = compute_activity(items, end, self.s.thresholds)
        tol = self.s.audit.metric_tolerance
        if not (_close(a["count_30d"], int(m["count_30d"]), tol) and _close(a["longform_60d"], int(m["longform_60d"]), tol)):
            return False, f"recomputed count_30d={a['count_30d']} longform_60d={a['longform_60d']} vs {m}"
        if not a["passed"]:
            return False, "recomputed activity below threshold"
        if self.live:
            from receipts.pipeline.s9_export import recheck

            lead = self.db.fetch_one("SELECT * FROM leads WHERE creator_key=?", (key,))
            fail = await recheck(self.ctx, lead, self.s, utcnow())
            if fail:
                return False, f"live re-check: {fail}"
        return True, ""

    async def sub_receipts(self, check, key, rec, items) -> tuple[bool, str]:
        agg = rec.get((check, ""))
        subs = [r for (c, sk), r in rec.items() if c == check and sk and r["result"] == "pass"]
        if not agg or agg["result"] != "pass" or not subs:
            return False, "no passing receipts"
        bad = []
        for r in subs:
            q = r["quote_or_metric"] or ""
            if METRIC_RE.match(q):
                ok, why = self.metric_signal(r, items)
            else:
                ok, why = await self.verify_receipt(r)
            if not ok:
                bad.append(f"{r['sub_key']}: {why}")
        if check == "needs":
            good = len(subs) - len(bad)
            if good < self.s.thresholds.need_min_signals:
                return False, "; ".join(bad) or "too few signals"
            return True, ("; ".join(bad) + " (rule still met)") if bad else ""
        return (not bad or len(bad) < len(subs)), "; ".join(bad)

    def metric_signal(self, r, items) -> tuple[bool, str]:
        m = parse_metric(r["quote_or_metric"])
        sub = r["sub_key"]
        end = parse_dt(r["checked_at"])
        thr = self.s.thresholds
        if "paidProductPlacement" in m:
            it = next((i for i in items if i.url == m.get("item")), None)
            return bool(it and it.paid_placement), "" if it and it.paid_placement else "flag not on item"
        if sub == "no_shortform":
            vids = [i for i in items if i.platform == "youtube" and within_days(i.published_at, thr.window_days_long, end)]
            shorts = [i for i in vids if i.duration_s and i.duration_s <= self.s.evidence.shorts_conservative_max_seconds]
            return (not shorts and bool(vids)), "" if not shorts else f"{len(shorts)} short items found"
        if sub == "captions_gap":
            ids = set(m.get("missing_ids", "").split(","))
            miss = [i for i in items if i.item_id in ids and i.has_captions is False]
            return len(miss) == len(ids), "" if len(miss) == len(ids) else "caption flags changed"
        if sub == "backlog":
            if "public_videos" in m:
                text = self.stored_text(r["snap_id"])
                ok = f'"videoCount": "{m["public_videos"]}"' in text
                return ok, "" if ok else "videoCount not in channel snapshot"
            return int(m.get("episodes", 0)) >= thr.backlog_min_items, ""
        if sub == "inconsistent_publishing":
            ok = m.get("max_gap_days") not in (None, "na") or int(m.get("prior_45d", 0)) > int(m.get("last_45d", 0))
            return ok, "" if ok else "no gap data"
        if sub == "single_platform":
            return r["snap_id"] is not None or bool(r["evidence_url"]), ""
        return True, ""

    async def reachable(self, key, rec) -> tuple[bool, str]:
        r = rec.get(("reachable", ""))
        best = self.db.fetch_one("SELECT * FROM channels WHERE creator_key=? AND role='best'", (key,))
        backup = self.db.fetch_one("SELECT * FROM channels WHERE creator_key=? AND role='backup'", (key,))
        if not r or r["result"] != "pass" or not best or not backup:
            return False, "best/backup missing"
        return await self.channel_ok(key, best)

    async def channel_ok(self, key, ch) -> tuple[bool, str]:
        try:
            assert_contact_provenance(ch["handle_or_url"], ch["source_url"], ch["source_quote"], ch["channel_type"])
        except GuardViolation as e:
            return False, str(e)
        if not ch["verified"]:
            return False, "channel not verified"
        if ch["channel_type"] in ("linkedin", "instagram", "x"):
            if not ch["manual_check_path"] or not Path(ch["manual_check_path"]).exists():
                return False, "manual check screenshot missing"
        elif ch["snap_id"] and not verify_quote(ch["source_quote"].split(" | manual note:")[0],
                                                self.stored_text(ch["snap_id"]), 1):
            return False, "source quote not in stored page"
        if ch["channel_type"] == "email":
            dom = ch["handle_or_url"].split("@")[-1]
            hosts = await mx_hosts(dom, fixture_mode=self.ctx.http.fixture_mode, fixtures_dir=self.s.fixtures_dir)
            if not hosts:
                return False, f"no MX for {dom}"
        if self.live and ch["channel_type"] not in ("linkedin", "instagram", "x"):
            code, text = await self.live_text(ch["source_url"])
            if code != 200:
                return False, f"source page status {code}"
            if ch["channel_type"] == "email" and ch["handle_or_url"].lower() not in text.lower():
                return False, "address no longer on source page"
        # name match: creator name/domain token on the source page, or handle matches
        cr = self.db.fetch_one("SELECT display_name, website_domain FROM creators WHERE creator_key=?", (key,))
        cc = CollectedCreator(candidate=Candidate(platform="website", source_id="x", url="https://x", source="audit"),
                              profile={"name": cr["display_name"],
                                       "website_url": f"https://{cr['website_domain']}" if cr["website_domain"] else None})
        page = normalise(self.stored_text(ch["snap_id"]))
        if not (any(t in page for t in name_tokens(cc)) or
                name_match_handle(ch["handle_or_url"], cc, self.s.identity.handle_name_match_ratio)):
            return False, "name does not match source"
        return True, ""

    def personalised(self, key, rec) -> tuple[bool, str]:
        r = rec.get(("personalised", ""))
        if not r or r["result"] != "pass":
            return False, f"receipt {r['reason_code'] if r else 'missing'}"
        msgs = self.db.fetch_all("SELECT * FROM messages WHERE creator_key=?", (key,))
        if not msgs or any(not m["validated"] for m in msgs):
            return False, "unvalidated message"
        item = self.db.fetch_one("SELECT * FROM content_items WHERE url=? AND creator_key=?", (msgs[0]["hook_source_url"], key))
        if not item or normalise(item["title"]) not in normalise(self.stored_text(item["snap_id"])):
            return False, "hook title not in snapshot"
        return True, ""

    def complete_unique(self, row: dict, dup_ids: set[str]) -> tuple[bool, str]:
        miss = missing_fields(row)
        if miss:
            return False, f"missing {miss}"
        if row["lead_id"] in dup_ids:
            return False, "duplicate"
        return True, ""

    async def audit_lead(self, lead, dup_ids) -> LeadAudit:
        key = lead["creator_key"]
        rec = {(r["check_name"], r["sub_key"]): r for r in self.db.fetch_all("SELECT * FROM receipts WHERE creator_key=?", (key,))}
        items = _items(self.db, key)
        la = LeadAudit(lead["lead_id"], key)
        results = {
            "real_relevant": await self.real_relevant(key, rec, items),
            "active": await self.active(key, rec, items),
            "commercial": await self.sub_receipts("commercial", key, rec, items),
            "needs": await self.sub_receipts("needs", key, rec, items),
            "reachable": await self.reachable(key, rec),
            "personalised": self.personalised(key, rec),
            "complete_unique": self.complete_unique(build_row(self.db, self.s, lead), dup_ids),
        }
        la.checks = {k: {"ok": bool(v[0]), "reason": v[1]} for k, v in results.items()}
        return la

async def run_audit(ctx, n: int, seed: int, channels: int, live: bool) -> dict:
    db, s = ctx.db, ctx.settings
    # real audits only sample leads whose every receipt is live; fixture-mode (demo) audits sample everything
    live = "" if ctx.http.fixture_mode else f" AND {creator_is_live('l')}"
    leads = db.fetch_all(f"SELECT * FROM leads l WHERE l.status='Qualified'{live} ORDER BY l.lead_id")
    rnd = random.Random(seed)
    sample = rnd.sample(list(leads), min(n, len(leads)))
    from receipts.checks.c7_complete_unique import duplicate_keys

    dup_ids = duplicate_keys([build_row(db, s, lead) for lead in leads])
    aud = Auditor(ctx, live)
    results = [await aud.audit_lead(lead, dup_ids) for lead in sample]
    ch_sample = rnd.sample(list(leads), min(channels, len(leads))) if channels else []
    ch_results = []
    for lead in ch_sample:
        best = db.fetch_one("SELECT * FROM channels WHERE creator_key=? AND role='best'", (lead["creator_key"],))
        ok, why = await aud.channel_ok(lead["creator_key"], best) if best else (False, "no best channel")
        ch_results.append({"lead_id": lead["lead_id"], "channel": best["channel_type"] if best else None,
                           "value": best["handle_or_url"] if best else None, "ok": ok, "reason": why})
    causes = Counter(f"{c}: {v['reason'].split(':')[0] or 'fail'}" for r in results for c, v in r.checks.items() if not v["ok"])
    causes.update(f"channel: {c['reason'].split(':')[0]}" for c in ch_results if not c["ok"])
    out = {
        "generated_at": utcnow().isoformat(), "seed": seed, "n": len(results), "live": live,
        "fixture_mode": ctx.http.fixture_mode,
        "pass_rate": (sum(r.passed for r in results) / len(results)) if results else 0.0,
        "per_check": {c: sum(r.checks[c]["ok"] for r in results) for c in CHECKS},
        "channel_n": len(ch_results),
        "channel_accuracy": (sum(c["ok"] for c in ch_results) / len(ch_results)) if ch_results else None,
        "root_causes": dict(causes.most_common()),
        "leads": [{"lead_id": r.lead_id, "creator_key": r.creator_key, "passed": r.passed, "checks": r.checks} for r in results],
        "channels": ch_results,
    }
    logs = s.out_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    stamp = utcnow().strftime("%Y%m%dT%H%M%S")
    (logs / f"audit_{stamp}.json").write_text(json.dumps(out, indent=2))
    write_audit_md(s.docs_dir / "self_audit.md", out, sorted(logs.glob("audit_*.json")))
    return out


def write_audit_md(path: Path, a: dict, history: list[Path]) -> Path:
    lines = ["# Self-audit", "",
             f"Latest: {a['generated_at'][:19]}Z · seed {a['seed']} · {a['n']} random leads · "
             f"{'live re-fetch' if a['live'] else 'stored snapshots'}"
             + (" · FIXTURE/SYNTHETIC DATA" if a.get("fixture_mode") else ""), "",
             f"- Lead pass rate (all 7 checks): **{a['pass_rate']:.0%}** (target ≥ 95%)",
             f"- Channel accuracy: **{a['channel_accuracy']:.0%}** on {a['channel_n']} channels" if a["channel_accuracy"] is not None
             else "- Channel accuracy: not sampled (use --channels 20)",
             "- Per check: " + ", ".join(f"{k} {v}/{a['n']}" for k, v in a["per_check"].items()), "",
             "## Failures", ""]
    fails = [(r["lead_id"], c, v["reason"]) for r in a["leads"] for c, v in r["checks"].items() if not v["ok"]]
    fails += [(c["lead_id"], f"channel {c['channel']}", c["reason"]) for c in a["channels"] if not c["ok"]]
    lines += [f"- {lid} · {c} · {why}" for lid, c, why in fails] or ["None."]
    lines += ["", "## Root causes", ""] + ([f"- {k}: {v}" for k, v in a["root_causes"].items()] or ["None."])
    lines += ["", "## History", "", "| When (UTC) | Seed | Leads | Pass rate | Channel accuracy | Mode |", "|---|---|---|---|---|---|"]
    for p in history[-20:]:
        h = json.loads(p.read_text())
        ca = f"{h['channel_accuracy']:.0%}" if h.get("channel_accuracy") is not None else "-"
        mode = ("live" if h["live"] else "stored") + (" (fixture)" if h.get("fixture_mode") else "")
        lines.append(f"| {h['generated_at'][:16]} | {h['seed']} | {h['n']} | {h['pass_rate']:.0%} | {ca} | {mode} |")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    return path

