"""Typer CLI, entry point `receipts` (BUILD_SPEC section 10)."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import typer
from rich.console import Console

from receipts.config import get_settings
from receipts.db import init_db

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Receipts Engine: creator leads with proof.")
console = Console()


@app.command()
def init() -> None:
    """Create out/, the DB, and copy example configs if missing."""
    s = get_settings()
    s.out_dir.mkdir(parents=True, exist_ok=True)
    init_db(s.db_path).close()
    if not Path(".env").exists() and Path(".env.example").exists():
        shutil.copy(".env.example", ".env")
        console.print("created .env from .env.example (fill in keys)")
    console.print(f"[green]initialised[/green] db={s.db_path} out={s.out_dir}")


@app.command()
def run(
    input: str = typer.Option("config/seeds.yaml", "--input", help="seeds .yaml, .csv, .txt, or comma keywords"),
    keywords: str = typer.Option("", "--keywords", help='ad-hoc discovery queries, "a,b"'),
    target: int = typer.Option(1200, "--target"),
    resume: bool = typer.Option(False, "--resume"),
    fixtures: bool = typer.Option(False, "--fixtures", help="replay recorded fixtures, no network"),
) -> None:
    """Full pipeline s0..s9."""
    import asyncio

    from receipts.pipeline.runner import run_pipeline

    if fixtures:
        input = _fixture_setup(input)
    kw = [k.strip() for k in keywords.split(",") if k.strip()] or None
    summary = asyncio.run(run_pipeline(input, target=target, resume=resume, kind="run", keywords=kw))
    console.print(str(summary))
    if summary.status == "partial":
        raise typer.Exit(3)


@app.command()
def discover(queries: int = typer.Option(10, "--queries")) -> None:
    """Only s1 discovery, within quota."""
    import asyncio

    from receipts.context import open_context
    from receipts.http import RUN_STOPPING
    from receipts.pipeline.s1_discover import discover as run_discover

    async def go() -> int:
        async with open_context() as ctx:
            try:
                return await run_discover(ctx, max_queries=queries)
            finally:
                console.print(f"youtube units spent: {ctx.yt.ledger.run_units}; remaining today: {ctx.yt.ledger.remaining}")

    try:
        n = asyncio.run(go())
    except RUN_STOPPING as e:
        console.print(f"[red]stopped:[/red] {e}")
        raise typer.Exit(3) from e
    console.print(f"new candidates: {n}")


SYNTH_DIR = Path("tests/fixtures/synthetic")
RECORDED_MANIFEST = Path("tests/fixtures/recorded_manifest.json")


def _fixture_setup(input: str, build: bool = True) -> str:
    """Replay mode. Real recorded fixtures if present, else a generated synthetic world (clearly labelled)."""
    import time

    os.environ["FIXTURE_MODE"] = "1"
    if RECORDED_MANIFEST.exists():
        os.environ.setdefault("RECEIPTS_FIXTURES", "tests/fixtures")
        console.print("[cyan]fixture mode: replaying REAL recorded responses (data_origin=fixture)[/cyan]")
    else:
        fresh = (SYNTH_DIR / "SYNTHETIC_README.txt").exists() and \
            time.time() - (SYNTH_DIR / "SYNTHETIC_README.txt").stat().st_mtime < 6 * 3600
        if build and not fresh:
            import sys
            from datetime import UTC, datetime

            sys.path.insert(0, str(Path.cwd()))  # scripts/ lives at the repo root, not in the package
            from scripts.make_synthetic import build_world, default_world, write_sample_input

            world = default_world()
            build_world(SYNTH_DIR, world, datetime.now(UTC))
            write_sample_input(SYNTH_DIR / "sample_input.csv", world)
        os.environ["RECEIPTS_FIXTURES"] = str(SYNTH_DIR)
        console.print("[yellow]fixture mode: no recorded real fixtures yet, replaying the SYNTHETIC test world "
                      "(data_origin=synthetic; never exportable to out/leads.csv). "
                      "Record real ones with scripts/record_fixtures.py.[/yellow]")
        if input == "tests/fixtures/sample_input.csv" and not Path(input).exists():
            input = str(SYNTH_DIR / "sample_input.csv")
    from receipts.config import get_settings as _gs

    _gs.cache_clear()
    return input


@app.command()
def refresh(input: str = typer.Option("config/seeds.yaml", "--input"),
            target: int = typer.Option(100_000, "--target"),
            fixtures: bool = typer.Option(False, "--fixtures")) -> None:
    """Upsert new input, TTL re-verify existing, print a diff report."""
    import asyncio

    from receipts.pipeline.runner import run_pipeline

    if fixtures:
        input = _fixture_setup(input)
    before = {r[0]: r[1] for r in init_db(get_settings().db_path).fetch_all("SELECT creator_key, status FROM creators")}
    summary = asyncio.run(run_pipeline(input, target=target, resume=False, kind="refresh"))
    db = init_db(get_settings().db_path)
    after = {r[0]: r[1] for r in db.fetch_all("SELECT creator_key, status FROM creators")}
    new = [k for k in after if k not in before and after[k] == "qualified"]
    dropped = [k for k in after if before.get(k) == "qualified" and after[k] != "qualified"]
    revived = [k for k in after if before.get(k) not in (None, "qualified") and after[k] == "qualified"]
    console.print(str(summary))
    console.print(f"diff: new qualified {len(new)}, re-verified {summary.updated}, newly rejected {len(dropped)}, "
                  f"re-qualified {len(revived)}, duplicates removed {summary.dupes_removed}")
    for k in dropped[:20]:
        console.print(f"  rejected {k}: {db.scalar('SELECT reject_reason FROM creators WHERE creator_key=?', (k,))}")
    if summary.status == "partial":
        raise typer.Exit(3)


@app.command()
def export(
    workbook: bool = typer.Option(False, "--workbook"),
    allow_imbalance: bool = typer.Option(False, "--allow-imbalance"),
    demo: bool = typer.Option(False, "--demo", help="keyless fixture demo: writes *_DEMO_not_live.csv only"),
) -> None:
    """Stage s9: guarded export of all outputs."""
    import asyncio

    from receipts.context import open_context
    from receipts.guards import GuardViolation
    from receipts.pipeline.s9_export import ExportBlocked, export_all
    from receipts.workbook import WorkbookMappingError

    if demo:
        os.environ["FIXTURE_MODE"] = "1"

    async def go():
        async with open_context(llm=None) as ctx:
            return await export_all(ctx, workbook=workbook, allow_imbalance=allow_imbalance, demo=demo)

    try:
        res = asyncio.run(go())
    except (GuardViolation, ExportBlocked, WorkbookMappingError, FileNotFoundError) as e:
        console.print(f"[red]export blocked:[/red] {e}")
        raise typer.Exit(1) from e
    if demo:
        console.print("[yellow]DEMO export: fixture/synthetic evidence; wrote *_DEMO_not_live.csv, not leads.csv[/yellow]")
    console.print(f"wrote {res.written} leads; dropped {res.dropped or 'none'}; messages {res.rerender}")
    for k, v in res.paths.items():
        console.print(f"  {k}: {v}")


@app.command()
def stats(
    dupes: bool = typer.Option(False, "--dupes"),
    balance: bool = typer.Option(False, "--balance"),
    priority: bool = typer.Option(False, "--priority"),
    include_non_live: bool = typer.Option(False, "--include-non-live",
                                          help="also count demo/fixture/synthetic data (never valid for acceptance)"),
) -> None:
    """Counts and acceptance-rule checks on real (live) data; non-zero exit if a rule fails."""
    from receipts.stats import compute

    s = get_settings()
    st = compute(init_db(s.db_path), s, live_only=not include_non_live)
    if include_non_live:
        console.print("[yellow]including demo/fixture/synthetic data: these numbers do not count for acceptance[/yellow]")
    console.print(f"qualified (selected): {st.qualified}   reserve: {st.reserve}   rejected candidates: {st.rejected}")
    console.print(f"by segment: {st.by_segment}")
    n = st.qualified or 1
    console.print("by country: " + ", ".join(f"{k} {v} ({v / n:.0%})" for k, v in st.by_country.items()))
    console.print(f"priority: {st.priority}   Priority A with 3 validated messages: {st.priority_a_full}")
    console.print(f"messages validated: {st.messages_validated}/{st.messages_total}   best-channel mix: {st.channel_mix}")
    if st.reject_reasons:
        console.print("reject reasons: " + ", ".join(f"{k}={v}" for k, v in list(st.reject_reasons.items())[:8]))
    if st.runs:
        r = st.runs[-1]
        tot_units = sum(x["youtube_units"] or 0 for x in st.runs)
        tot_cost = sum((x["llm_cost_usd"] or 0) + (x["direct_cost_usd"] or 0) for x in st.runs)
        console.print(f"runs: {len(st.runs)} (last: {r['kind']} {r['status']}); YouTube units total {tot_units}; "
                      f"cost total ${tot_cost:.4f}")
    if dupes:
        console.print(f"duplicates: {st.dupes}")
    show = [r for r in st.rules if (not (dupes or balance or priority))
            or (dupes and r.code == "A2") or (balance and r.code.startswith("A5")) or (priority and r.code == "A6")]
    failed = False
    for r in show:
        console.print(f"  [{'green' if r.ok else 'red'}]{'PASS' if r.ok else 'FAIL'}[/] {r.code} {r.desc}: {r.value}")
        failed |= not r.ok
    if failed:
        raise typer.Exit(1)


@app.command()
def trace(lead_id: str, as_json: bool = typer.Option(False, "--json")) -> None:
    """Print all 7 receipts for a lead."""
    from receipts.trace import TraceError, print_trace

    s = get_settings()
    try:
        print_trace(init_db(s.db_path), s, lead_id, as_json, console)
    except TraceError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e


@app.command()
def audit(
    n: int = typer.Option(20, "--n"),
    seed: int = typer.Option(7, "--seed"),
    channels: int = typer.Option(0, "--channels"),
    live: bool = typer.Option(False, "--live"),
    fixtures: bool = typer.Option(False, "--fixtures", help="replay fixtures (demo DBs)"),
) -> None:
    """Random self-audit of leads and channels."""
    import asyncio

    if fixtures:
        _fixture_setup("", build=False)

    from receipts.audit import run_audit
    from receipts.context import open_context

    async def go():
        async with open_context(llm=None) as ctx:
            return await run_audit(ctx, n, seed, channels, live)

    a = asyncio.run(go())
    if a["n"] == 0:
        console.print("[red]no exported/qualified leads to audit[/red]")
        raise typer.Exit(1)
    console.print(f"audit seed={a['seed']} leads={a['n']} pass rate={a['pass_rate']:.0%} "
                  f"per check={a['per_check']}")
    if a["channel_accuracy"] is not None:
        console.print(f"channel accuracy={a['channel_accuracy']:.0%} on {a['channel_n']}")
    for k, v in list(a["root_causes"].items())[:10]:
        console.print(f"  cause {k}: {v}")
    console.print(f"report: {get_settings().docs_dir / 'self_audit.md'}")
    if a["pass_rate"] < 0.95 or (a["channel_accuracy"] is not None and a["channel_accuracy"] < 0.95):
        raise typer.Exit(1)


@app.command("manual-check")
def manual_check(
    lead_id: str,
    channel: str = typer.Option(..., "--channel"),
    active_date: str = typer.Option(..., "--active-date"),
    screenshot: Path = typer.Option(..., "--screenshot"),
    note: str = typer.Option("", "--note"),
) -> None:
    """Record manual proof for a LinkedIn/Instagram/X channel."""
    from receipts.manual import ManualCheckError, record_manual_check
    from receipts.util.dates import utcnow

    s = get_settings()
    db = init_db(s.db_path)
    try:
        res = record_manual_check(db, s, lead_id, channel, active_date, screenshot, note, utcnow())
    except (ManualCheckError, ValueError) as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e
    console.print(res)
    if not res["verified"]:
        console.print("[yellow]recorded, but not verified (name does not match or active date older than "
                      f"{s.channels.active_window_days} days)[/yellow]")


@app.command("seed-test")
def seed_test(
    channel: str = typer.Option("all", "--channel", help="email|form|linkedin|community|all"),
    all_: bool = typer.Option(False, "--all"),
    tick: bool = typer.Option(False, "--tick"),
) -> None:
    """Seed test on owned accounts only (G2)."""
    from receipts.outreach import seedtest as st
    from receipts.outreach.replies import poll_imap
    from receipts.util.dates import utcnow

    s = get_settings()
    db = st.seed_db(s)
    now = utcnow()
    chans = list(st.CHANNELS) if (all_ or channel == "all") else [channel]
    if (all_ or channel == "all") and not os.environ.get(s.seed_test.community_webhook_env):
        chans.remove("community")  # optional fallback channel: only when its webhook is configured
    if channel not in (*st.CHANNELS, "all"):
        console.print(f"[red]unknown channel {channel}[/red]")
        raise typer.Exit(2)
    miss = st.prerequisites(s)
    notes: list[str] = []
    if not tick:
        n = st.schedule(db, s, chans, now)
        console.print(f"scheduled {n} message(s) for test leads {', '.join(st.TEST_LEADS)} on {', '.join(chans)} "
                      f"(follow_up_days {s.follow_up_days} compressed x{s.seed_test.time_compression_factor:g})")
    senders = st.Senders(s)
    res = st.tick(db, s, now, senders)
    console.print(f"tick: {res}")
    notes += senders.log
    try:
        replies = poll_imap()
        console.print(f"replies matched: {st.apply_replies(db, replies, now)}")
    except RuntimeError as e:
        notes.append(str(e))
    except Exception as e:  # IMAP login/network errors are reported, not fatal
        notes.append(f"IMAP poll failed: {type(e).__name__}: {e}")
    for ch in chans:
        if miss.get(ch):
            notes.append(f"{ch}: missing {', '.join(miss[ch])}")
    path = st.write_report(db, s, notes)
    notes = list(dict.fromkeys(notes))
    for ch, due in st.due_summary(db):
        console.print(f"  next {ch} step due {due[:19]}Z (run `receipts seed-test --tick` after that)")
    for n_ in notes:
        console.print(f"[yellow]{n_}[/yellow]")
    console.print(f"report: {path}")
    blocking = [ch for ch in chans if miss.get(ch) and ch != "community"]
    if blocking:
        console.print(f"[red]seed test incomplete: provide what is listed for {', '.join(blocking)} "
                      "(BUILD_SPEC section 16 items 2-3)[/red]")
        raise typer.Exit(1)


@app.command("log-reply")
def log_reply(
    lead_id: str,
    channel: str = typer.Option(..., "--channel"),
    label: str = typer.Option(..., "--label"),
) -> None:
    """Log a reply received on an assisted channel."""
    from receipts.outreach import seedtest as st
    from receipts.util.dates import utcnow

    if label not in ("positive", "negative", "stop", "auto_reply", "other"):
        console.print("[red]label must be positive|negative|stop|auto_reply|other[/red]")
        raise typer.Exit(2)
    db = st.seed_db(get_settings())
    st.record_reply(db, lead_id, channel, label, "logged manually", "manual", utcnow())
    console.print(f"logged {label} reply for {lead_id} on {channel}"
                  + ("; remaining steps cancelled" if label in ("stop", "negative") else ""))


@app.command("log-sent")
def log_sent(lead_id: str, channel: str = typer.Option(..., "--channel"),
             step: int = typer.Option(..., "--step")) -> None:
    """Mark an assisted-send message as sent by the human."""
    from receipts.outreach import seedtest as st
    from receipts.util.dates import utcnow

    ok = st.log_sent(st.seed_db(get_settings()), lead_id, channel, step, utcnow())
    console.print("marked sent" if ok else "[red]no queued message matches[/red]")
    if not ok:
        raise typer.Exit(1)


@app.command()
def tracker(port: int = typer.Option(8000, "--port")) -> None:
    """Run the redirect/form endpoint."""
    import uvicorn

    from receipts.outreach.tracker import create_app

    uvicorn.run(create_app(get_settings()), host="127.0.0.1", port=port)


@app.command()
def ui(port: int = typer.Option(8080, "--port"), host: str = typer.Option("127.0.0.1", "--host")) -> None:
    """Local operator console: dashboard, leads with receipts, manual checks, rejects, runs, seed test."""
    import uvicorn

    from receipts.web.app import create_app

    if host not in ("127.0.0.1", "localhost"):
        console.print("[yellow]warning: the console has no login; bind it to 127.0.0.1 unless you trust the network[/yellow]")
    console.print(f"Receipts console on http://{host}:{port}  (db: {get_settings().db_path})")
    uvicorn.run(create_app(get_settings()), host=host, port=port, log_level="warning")


@app.command()
def viewer() -> None:
    """Rebuild the static viewer."""
    from receipts.leadrow import build_row
    from receipts.viewer import build_viewer

    s = get_settings()
    db = init_db(s.db_path)
    rows = [build_row(db, s, r) for r in db.fetch_all("SELECT * FROM leads WHERE status='Qualified' ORDER BY lead_id")]
    demo = os.environ.get("FIXTURE_MODE") == "1" or bool(db.scalar("SELECT 1 FROM receipts WHERE data_origin != 'live'"))
    console.print(f"viewer: {build_viewer(db, s, s.out_dir / 'viewer', rows, demo=demo)}/index.html")


@app.command("outreach-check")
def outreach_check() -> None:
    """Verify 3 segments x 3 channels x 3 steps templates exist and validate."""
    from rich.table import Table

    from receipts.outreach.check import run_check

    rows, ok, sample = run_check(get_settings())
    t = Table("segment", "channel", "step", "exists", "valid", "failures")
    for r in rows:
        t.add_row(r["segment"], r["channel"], str(r["step"]), "yes" if r["exists"] else "NO",
                  "yes" if r["valid"] else "NO", r["failures"])
    console.print(t)
    if sample:
        console.print("[yellow]sender/offer_line still contain TODO: validated with a labelled sample sender. "
                      "Real messages stay unvalidated until settings.yaml is filled.[/yellow]")
    segs = {r["segment"] for r in rows if r["valid"]}
    chans = {r["channel"] for r in rows if r["valid"]}
    console.print(f"{sum(r['valid'] for r in rows)}/27 templates valid; segments={len(segs)} channels={len(chans)}")
    if not ok:
        raise typer.Exit(1)


@app.command()
def bench(rows: int = typer.Option(5000, "--rows"), fixtures: bool = typer.Option(False, "--fixtures")) -> None:
    """Synthetic replay benchmark (labelled synthetic, not live)."""
    from receipts.bench import run_bench, write_bench_report

    if not fixtures:
        console.print("[red]bench only runs as a synthetic fixture replay: add --fixtures[/red]")
        raise typer.Exit(2)
    docs = Path(os.environ.get("RECEIPTS_DOCS", "docs"))
    res = run_bench(rows, get_settings())
    path = write_bench_report(res, docs / "bench_report.md")
    console.print(f"[yellow]{res['label']}[/yellow]")
    for k, v in res.items():
        if k != "label":
            console.print(f"  {k}: {v}")
    console.print(f"report: {path}")
    if res["duplicates_remaining"]:
        raise typer.Exit(1)


@app.command()
def forget(creator_key: str) -> None:
    """Delete a creator's data and snapshots tied only to them."""
    from receipts.forget import forget as do_forget

    s = get_settings()
    res = do_forget(init_db(s.db_path), creator_key)
    if not res["found"]:
        console.print(f"[red]no creator {creator_key}[/red]")
        raise typer.Exit(1)
    console.print(f"forgotten {creator_key}: {res}. Re-run `receipts export` to rewrite output files.")


@app.command()
def report() -> None:
    """Regenerate docs/run_report.md from runs."""
    from receipts.report import write_run_report

    s = get_settings()
    console.print(str(write_run_report(init_db(s.db_path), s.docs_dir / "run_report.md")))


if __name__ == "__main__":
    app()
