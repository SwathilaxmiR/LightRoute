"""Record REAL API/web responses as replay fixtures (run once by the human, with keys in .env).

It runs the actual pipeline (stages s0..s8) on the targets in tests/fixtures/record_targets.yaml
with RECORD_FIXTURES=1, so every request the pipeline makes is written to tests/fixtures/http/
(API keys are stripped from stored URLs). It then writes:
  tests/fixtures/sample_input.csv       the input `make demo` replays
  tests/fixtures/recorded_manifest.json what tests/test_sources.py checks

Recorded fixtures replay as data_origin='fixture': usable for demo and tests, never for out/leads.csv (G1).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    targets_path = ROOT / "tests/fixtures/record_targets.yaml"
    t = yaml.safe_load(targets_path.read_text()) or {}
    todo = [f"{k}[{i}]" for k in ("youtube", "podcast", "website") for i, v in enumerate(t.get(k, [])) if "TODO" in str(v)]
    if todo:
        print(f"Fill {targets_path.relative_to(ROOT)} first; still TODO: {', '.join(todo)}")
        return 2
    missing = [k for k in ("YOUTUBE_API_KEY", "PODCASTINDEX_KEY", "PODCASTINDEX_SECRET") if not os.environ.get(k)]
    if missing:
        print(f"Missing keys in .env: {', '.join(missing)}")
        return 2

    fixtures = ROOT / "tests/fixtures"
    work = ROOT / "out/record"
    work.mkdir(parents=True, exist_ok=True)
    inp = work / "record_input.csv"
    rows = ["url"]
    rows += [v if str(v).startswith(("http", "@", "UC")) else f"https://www.youtube.com/{v}" for v in t["youtube"]]
    rows += [f"pi:{v}" for v in t["podcast"]]
    rows += list(t["website"])
    inp.write_text("\n".join(str(r) for r in rows) + "\n")

    os.environ.update({"RECORD_FIXTURES": "1", "FIXTURE_MODE": "0", "RECEIPTS_DB": str(work / "state.db"),
                       "RECEIPTS_OUT": str(work), "RECEIPTS_FIXTURES": str(fixtures)})
    from receipts.config import get_settings
    from receipts.db import init_db
    from receipts.pipeline.runner import run_pipeline

    get_settings.cache_clear()
    summary = asyncio.run(run_pipeline(str(inp), target=10_000, resume=False, kind="record", stop_after="s8"))
    print(summary)

    db = init_db(work / "state.db")
    yt = [r["source_id"] for r in db.fetch_all("SELECT source_id FROM candidates WHERE platform='youtube' ORDER BY cand_id")]
    pi = [int(r["source_id"]) for r in db.fetch_all("SELECT source_id FROM candidates WHERE platform='podcast' ORDER BY cand_id")]
    web = [r["url"] for r in db.fetch_all("SELECT url FROM candidates WHERE platform='website' ORDER BY cand_id")]
    manifest = {"youtube": yt, "podcast": pi, "website": web}
    (fixtures / "recorded_manifest.json").write_text(json.dumps(manifest, indent=2))
    sample = ["url"] + [f"https://www.youtube.com/channel/{c}" for c in yt] + [f"pi:{p}" for p in pi] + web
    (fixtures / "sample_input.csv").write_text("\n".join(sample) + "\n")
    n = len(list((fixtures / "http").glob("*.json")))
    print(f"recorded {n} responses; manifest: {manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
