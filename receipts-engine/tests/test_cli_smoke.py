from __future__ import annotations

import time

import pytest
from typer.testing import CliRunner

from receipts.cli import app

COMMANDS = ["init", "run", "discover", "refresh", "export", "stats", "trace", "audit", "manual-check", "seed-test",
            "log-reply", "log-sent", "tracker", "ui", "viewer", "outreach-check", "bench", "forget", "report"]
runner = CliRunner()


@pytest.mark.parametrize("cmd", COMMANDS)
def test_every_command_has_help(cmd):
    r = runner.invoke(app, [cmd, "--help"])
    assert r.exit_code == 0, r.output
    assert "Usage" in r.output


def test_demo_equivalent_runs_under_5_minutes(monkeypatch, tmp_path):
    """Same steps as `make demo`: fixture run, demo export, trace, audit (no keys, no network)."""
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    t0 = time.perf_counter()
    r = runner.invoke(app, ["run", "--input", "tests/fixtures/sample_input.csv", "--fixtures", "--target", "50"])
    assert r.exit_code == 0, r.output
    assert "produced=" in r.output
    r = runner.invoke(app, ["export", "--demo"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["trace", "L0001"])
    assert r.exit_code == 0 and "real_relevant" in r.output, r.output
    r = runner.invoke(app, ["audit", "--n", "20", "--seed", "7", "--fixtures"])
    assert "pass rate" in r.output, r.output
    r = runner.invoke(app, ["outreach-check"])
    assert r.exit_code == 0, r.output
    assert time.perf_counter() - t0 < 300
