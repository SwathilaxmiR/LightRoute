from __future__ import annotations

from pathlib import Path

import pytest

from receipts.config import get_settings, load_settings
from receipts.db import init_db

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"


@pytest.fixture(autouse=True)
def _isolated_paths(tmp_path, monkeypatch):
    """Every test gets its own DB and out/ dir; config is read from the repo."""
    monkeypatch.chdir(ROOT)
    monkeypatch.setenv("RECEIPTS_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("RECEIPTS_OUT", str(tmp_path / "out"))
    monkeypatch.setenv("FIXTURE_MODE", "0")
    monkeypatch.setenv("RECEIPTS_DOCS", str(tmp_path / "docs"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def settings():
    return load_settings(ROOT / "config")


@pytest.fixture
def db(tmp_path):
    d = init_db(tmp_path / "state.db")
    yield d
    d.close()


@pytest.fixture
def run_world(tmp_path, monkeypatch):
    """Build a synthetic fixture world and run the real pipeline on it in replay mode.

    usage: summary, db = run_world(creators, refs, stop_after="s8", **run_kwargs)
    """
    import asyncio
    from datetime import UTC, datetime

    from receipts.pipeline.runner import run_pipeline
    from scripts.make_synthetic import build_world

    fx = tmp_path / "fx"
    monkeypatch.setenv("FIXTURE_MODE", "1")
    monkeypatch.setenv("RECEIPTS_FIXTURES", str(fx))
    opened: list = []

    def _run(creators, refs, *, build=True, target=10_000, resume=False, stop_after=None, kind="run", **kw):
        if build:
            build_world(fx, creators, datetime.now(UTC))
        inp = tmp_path / f"input_{len(opened)}.csv"
        inp.write_text("url\n" + "\n".join(refs) + "\n")
        get_settings.cache_clear()
        summary = asyncio.run(run_pipeline(str(inp), target=target, resume=resume, kind=kind,
                                           stop_after=stop_after, llm=None, **kw))
        d = init_db(get_settings().db_path)
        opened.append(d)
        return summary, d

    yield _run
    for d in opened:
        d.close()


@pytest.fixture
def filled_config(tmp_path, monkeypatch):
    """A copy of config/ with the human-provided TODO fields filled with clearly-sample values."""
    import shutil

    d = tmp_path / "config"
    shutil.copytree(ROOT / "config", d)
    p = d / "settings.yaml"
    t = p.read_text()
    t = t.replace('name: "TODO"', 'name: "Sam Sample"').replace('postal_address: "TODO"', 'postal_address: "1 Sample Street, Testville"')
    t = t.replace('reply_to: "TODO"', 'reply_to: "sam@sample.test"')
    t = t.replace('offer_line: "TODO"', 'offer_line: "IncuBrix helps creators turn long videos into short clips."')
    t = t.replace("TODO_EMAIL", "sam@sample.test")
    t = t.replace('"TODO_TEST_ADDRESS_1", "TODO_TEST_ADDRESS_2"', '"me@sample.test", "me2@sample.test"')
    p.write_text(t)
    monkeypatch.setenv("RECEIPTS_CONFIG_DIR", str(d))
    get_settings.cache_clear()
    return d
