"""Pipeline orchestration: stages, per-candidate commits, resume, graceful quota stop (BUILD_SPEC 8.9)."""
from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass, field

import httpx

from receipts.config import get_settings
from receipts.context import Ctx, open_context
from receipts.db import DB, init_db
from receipts.http import RUN_STOPPING, TRANSIENT, FixtureMissing
from receipts.pipeline.common import Reject, save_receipt, worst_origin
from receipts.pipeline.s0_ingest import insert_candidates, insert_input_rejects, load_input, row_to_candidate
from receipts.pipeline.s1_discover import discover
from receipts.pipeline.s2_collect import collect
from receipts.pipeline.s3_filter import run_filters
from receipts.pipeline.s5_identity import (
    anchor,
    find_owner,
    identities,
    make_key,
    merge_into,
    primary_platform,
    review_fuzzy,
    save_identities,
    save_items,
    upsert_creator,
)
from receipts.sources.youtube import YouTubeError
from receipts.util.dates import utcnow
from receipts.util.logging import JsonlLogger

STAGE_ORDER = ["s0", "s1", "s2", "s3", "s4", "s5", "s6", "s7", "s8", "s9"]


@dataclass
class RunSummary:
    run_id: int
    kind: str
    status: str = "running"
    input_size: int = 0
    new_candidates: int = 0
    processed: int = 0
    produced: int = 0
    rejected: int = 0
    deferred: int = 0
    dupes_removed: int = 0
    unchanged: int = 0
    updated: int = 0
    qualified_total: int = 0
    reverify: int = 0
    youtube_units: int = 0
    message: str = ""
    reasons: dict[str, int] = field(default_factory=dict)

    def __str__(self) -> str:
        d = asdict(self)
        reasons = d.pop("reasons")
        msg = d.pop("message")
        s = "  ".join(f"{k}={v}" for k, v in d.items())
        if reasons:
            s += "\n  reject reasons: " + ", ".join(f"{k}={v}" for k, v in sorted(reasons.items(), key=lambda x: -x[1]))
        return s + (f"\n{msg}" if msg else "")


def _reached(stop_after: str | None, stage: str) -> bool:
    """True if `stage` should NOT run because stop_after comes before it."""
    return stop_after is not None and STAGE_ORDER.index(stage) > STAGE_ORDER.index(stop_after)


def qualified_count(db: DB) -> int:
    return db.scalar("SELECT COUNT(*) FROM creators WHERE status='qualified'") or 0


def reopen_stale(db: DB, s, now) -> int:
    """Refresh: reopen the primary candidate of every creator not verified within ttl_refresh_days.
    Qualified and rejected creators are both re-checked (a rejected one may have become active again)."""
    from datetime import timedelta

    cutoff = (now - timedelta(days=s.freshness.ttl_refresh_days)).isoformat()
    rows = db.fetch_all(
        "SELECT ca.cand_id, ca.hint FROM candidates ca JOIN creators c ON c.creator_key = ca.creator_key "
        "WHERE (c.last_verified IS NULL OR c.last_verified < ?) AND ca.platform != 'input' "
        "AND (ca.reject_reason IS NULL OR ca.reject_reason NOT IN ('duplicate_merged','forgotten'))", (cutoff,))
    with db.transaction():
        for r in rows:
            hint = json.loads(r["hint"] or "{}")
            hint["refresh"] = True
            db.execute("UPDATE candidates SET status='open', hint=? WHERE cand_id=?", (json.dumps(hint), r["cand_id"]))
    return len(rows)


def _reject(db: DB, cand_id: int, key: str | None, reason: str, stage: str, run_id: int | None,
            detail: str = "") -> None:
    with db.transaction():
        db.execute("UPDATE candidates SET status='rejected', stage=?, reject_reason=?, reject_detail=?, rejected_at=?, "
                   "run_id=? WHERE cand_id=?", (stage, reason, detail[:300], utcnow().isoformat(), run_id, cand_id))
        if key:
            db.execute("UPDATE creators SET status='rejected', reject_reason=? WHERE creator_key=?", (reason, key))
            db.execute("UPDATE leads SET status='Rejected' WHERE creator_key=?", (key,))  # lead ids are never reused


async def process_candidate(ctx: Ctx, row, stop_after: str | None, summary: RunSummary) -> None:
    db, s = ctx.db, ctx.settings
    cand_id = row["cand_id"]
    cand = row_to_candidate(row)
    key: str | None = row["creator_key"]
    stage = "collected"
    try:
        c = await collect(ctx, cand)
        shared = s.exclusions.get("shared_hosts", [])
        key = make_key(anchor(c, shared, s.exclusions.get("domains_blocklist", [])))
        ids = identities(c, s)
        owner = find_owner(db, ids, key, cand_id)
        with db.transaction():
            if owner is not None:
                merge_into(db, owner, c, ids)
                db.execute("UPDATE candidates SET status='done', stage='identified', reject_reason='duplicate_merged', "
                           "creator_key=?, run_id=? WHERE cand_id=?", (owner, ctx.run_id, cand_id))
                summary.dupes_removed += 1
                ctx.log.log("duplicate_merged", cand_id=cand_id, into=owner)
                return
            upsert_creator(db, key, c, s, ctx.now)
            save_identities(db, key, ids)
            save_items(db, key, c)
            db.execute("UPDATE candidates SET stage='collected', creator_key=?, run_id=? WHERE cand_id=?",
                       (key, ctx.run_id, cand_id))
        review_fuzzy(db, key, c.profile.get("name", ""), s.out_dir, s.identity.fuzzy_review_ratio)
        origin = worst_origin(db, c.snap_ids)

        if _reached(stop_after, "s3"):
            return
        stage = "filtered"
        fr = run_filters(ctx, c)
        with db.transaction():
            for r in fr.receipts:
                save_receipt(db, key, r, origin)
        if not fr.ok:
            raise Reject(fr.reason or "filtered", stage=stage)
        plat, pid = primary_platform(c, fr.segment, ctx.now, s.thresholds.window_days_long)
        with db.transaction():
            db.execute("UPDATE creators SET segment=?, country_iso2=?, primary_language='en', primary_platform=?, "
                       "primary_id=? WHERE creator_key=?",
                       (fr.segment, fr.country, plat, pid, key))
            db.execute("UPDATE candidates SET stage='filtered' WHERE cand_id=?", (cand_id,))

        for st in ("s4", "s6", "s7", "s8"):
            if _reached(stop_after, st):
                break
            fn = STAGE_FUNCS.get(st)
            if fn is None:
                continue
            stage = STAGE_NAMES[st]
            await fn(ctx, c, key, fr, origin)
            db.execute("UPDATE candidates SET stage=? WHERE cand_id=?", (stage, cand_id))

        with db.transaction():
            db.execute("UPDATE creators SET status='qualified', reject_reason=NULL, last_verified=? WHERE creator_key=?",
                       (ctx.now.isoformat(), key))
            db.execute("UPDATE candidates SET status='done', reject_reason=NULL WHERE cand_id=?", (cand_id,))
        if json.loads(row["hint"] or "{}").get("refresh"):
            summary.updated += 1
        else:
            summary.produced += 1
    except Reject as e:
        _reject(db, cand_id, key, e.reason, e.stage or stage, ctx.run_id, e.detail)
        summary.rejected += 1
        summary.reasons[e.reason] = summary.reasons.get(e.reason, 0) + 1
        ctx.log.log("rejected", cand_id=cand_id, reason=e.reason, detail=e.detail, stage=e.stage or stage)
    except FixtureMissing as e:
        _reject(db, cand_id, key, "fixture_missing", stage, ctx.run_id, str(e))
        summary.rejected += 1
        summary.reasons["fixture_missing"] = summary.reasons.get("fixture_missing", 0) + 1
        ctx.log.log("rejected", cand_id=cand_id, reason="fixture_missing", detail=str(e))
    except RUN_STOPPING:
        db.execute("UPDATE candidates SET status='deferred', run_id=? WHERE cand_id=?", (ctx.run_id, cand_id))
        summary.deferred += 1
        raise
    except (*TRANSIENT, httpx.HTTPError) as e:
        db.execute("UPDATE candidates SET status='deferred', reject_reason=?, run_id=? WHERE cand_id=?",
                   (f"transient:{type(e).__name__}", ctx.run_id, cand_id))
        summary.deferred += 1
        ctx.log.error(stage, cand_id, e)
    except YouTubeError as e:
        _reject(db, cand_id, key, "source_error", stage, ctx.run_id)
        summary.rejected += 1
        summary.reasons["source_error"] = summary.reasons.get("source_error", 0) + 1
        ctx.log.error(stage, cand_id, e)
    except Exception as e:  # a bug must not abort the whole run; it is visible in rejects.csv and the log
        _reject(db, cand_id, key, f"internal_error:{type(e).__name__}", stage, ctx.run_id, str(e))
        summary.rejected += 1
        summary.reasons["internal_error"] = summary.reasons.get("internal_error", 0) + 1
        ctx.log.error(stage, cand_id, e)


STAGE_FUNCS: dict = {}
STAGE_NAMES = {"s4": "evidenced", "s6": "channelled", "s7": "scored", "s8": "messaged"}


def register_stage(name: str):
    def deco(fn):
        STAGE_FUNCS[name] = fn
        return fn
    return deco


def _load_stage_modules() -> None:
    import importlib

    for mod in ("s4_evidence", "s6_channels", "s7_score", "s8_messages"):
        try:
            importlib.import_module(f"receipts.pipeline.{mod}")
        except ModuleNotFoundError:
            pass


async def run_pipeline(input_spec: str | None, *, target: int, resume: bool, kind: str = "run",
                       keywords: list[str] | None = None, stop_after: str | None = None,
                       max_queries: int | None = None, transport=None, llm="auto") -> RunSummary:
    _load_stage_modules()
    s = get_settings()
    db = init_db(s.db_path)
    started = utcnow()
    run_id = db.insert("runs", {"kind": kind, "started": started.isoformat(), "input_desc": str(input_spec or keywords),
                                "status": "running"})
    log_path = s.out_dir / "logs" / f"run_{run_id}.jsonl"
    db.execute("UPDATE runs SET log_path=? WHERE run_id=?", (str(log_path), run_id))
    summary = RunSummary(run_id=run_id, kind=kind)
    stopped: Exception | None = None

    async with open_context(s, db=db, transport=transport, llm=llm) as ctx:
        ctx.run_id = run_id
        db.execute("UPDATE runs SET data_origin=? WHERE run_id=?", ("fixture" if ctx.http.fixture_mode else "live", run_id))
        ctx.log = JsonlLogger(log_path)
        ctx.log.log("run_start", kind=kind, input=str(input_spec), target=target, resume=resume,
                    fixture_mode=ctx.http.fixture_mode)

        inp = load_input(input_spec, keywords, s.exclusions.get("shared_hosts", []))
        summary.input_size = len(inp.candidates) + len(inp.rejects)
        summary.new_candidates = insert_candidates(db, inp.candidates, run_id)
        insert_input_rejects(db, inp.rejects, run_id)
        if inp.candidates:
            db.execute("CREATE TEMP TABLE IF NOT EXISTS _input (platform TEXT, source_id TEXT)")
            db.execute("DELETE FROM _input")
            db.conn.executemany("INSERT INTO _input VALUES (?, ?)", [(c.platform, c.source_id) for c in inp.candidates])
            summary.unchanged = db.scalar(
                "SELECT COUNT(*) FROM candidates ca JOIN _input i ON i.platform=ca.platform AND i.source_id=ca.source_id "
                "WHERE ca.status IN ('done','rejected') AND (ca.run_id IS NULL OR ca.run_id != ?)", (run_id,)) or 0

        if (inp.is_seeds or inp.keywords) and not _reached(stop_after, "s2") and qualified_count(db) < target:
            try:
                summary.new_candidates += await discover(ctx, keywords=inp.keywords, max_queries=max_queries)
            except RUN_STOPPING as e:
                stopped = e

        if kind == "refresh":
            summary.reverify = reopen_stale(db, s, ctx.now)
            ctx.log.log("refresh_reopened", n=summary.reverify)

        statuses = ("open", "deferred") if resume else ("open",)
        rows = db.fetch_all(
            f"SELECT * FROM candidates WHERE status IN ({','.join('?' * len(statuses))}) AND platform != 'input' "
            "ORDER BY cand_id", statuses)
        queue: asyncio.Queue = asyncio.Queue()
        for r in rows if not _reached(stop_after, "s2") else []:
            queue.put_nowait(r)
        stop_flag = asyncio.Event()

        async def worker() -> None:
            nonlocal stopped
            while not queue.empty() and not stop_flag.is_set():
                if qualified_count(db) >= target:
                    stop_flag.set()
                    return
                r = queue.get_nowait()
                summary.processed += 1
                try:
                    await process_candidate(ctx, r, stop_after, summary)
                except RUN_STOPPING as e:
                    stopped = e
                    stop_flag.set()

        await asyncio.gather(*(worker() for _ in range(max(1, s.pipeline.candidate_concurrency))))

        if stopped is not None:
            # leave everything not yet processed for --resume
            remaining = []
            while not queue.empty():
                remaining.append(queue.get_nowait()["cand_id"])
            if remaining:
                db.execute(f"UPDATE candidates SET status='deferred' WHERE cand_id IN ({','.join('?' * len(remaining))}) "
                           "AND status='open'", remaining)
            summary.deferred += len(remaining)
            summary.status = "partial"
            summary.message = (f"STOPPED: {stopped}\n{summary.deferred} candidate(s) deferred. "
                               f"Fix the cause (wait for quota reset or correct the key in .env), then run:\n"
                               f"  receipts run --input {input_spec or 'config/seeds.yaml'} --target {target} --resume")
        else:
            summary.status = "ok"

        if not _reached(stop_after, "s7") and "s7" in STAGE_FUNCS:
            from receipts.pipeline.s7_score import select_final

            bal = select_final(db, s, s.out_dir / "logs" / "balance.json")
            ctx.log.log("selection", selected=bal.selected, violations=bal.violations, priority=bal.priority)

        summary.youtube_units = ctx.yt.ledger.run_units
        summary.qualified_total = qualified_count(db)
        llm = ctx.llm
        db.execute(
            "UPDATE runs SET ended=?, input_size=?, produced=?, rejected=?, dupes_removed=?, unchanged=?, updated=?, "
            "youtube_units=?, llm_in_tokens=?, llm_out_tokens=?, llm_cost_usd=?, status=?, note=? WHERE run_id=?",
            (utcnow().isoformat(), summary.input_size, summary.produced, summary.rejected, summary.dupes_removed,
             summary.unchanged, summary.updated, summary.youtube_units, getattr(llm, "in_tokens", 0),
             getattr(llm, "out_tokens", 0), getattr(llm, "cost_usd", 0.0), summary.status,
             json.dumps({"reasons": summary.reasons, "deferred": summary.deferred, "processed": summary.processed}),
             run_id))
        ctx.log.log("run_end", **{k: v for k, v in asdict(summary).items() if k != "message"})
    from receipts.report import write_run_report

    write_run_report(db, s.docs_dir / "run_report.md")
    return summary
