"""Non-negotiable rules G1..G8 as code (BUILD_SPEC section 2).

Every guard raises a GuardViolation subclass; callers never catch these silently.
"""
from __future__ import annotations

import fnmatch
import os
import re
import subprocess
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib import robotparser

import yaml


class GuardViolation(Exception):
    guard = "G?"


class DataOriginViolation(GuardViolation):
    guard = "G1"


class RecipientNotAllowed(GuardViolation):
    guard = "G2"


class ProvenanceMissing(GuardViolation):
    guard = "G3"


class ConfigIncomplete(GuardViolation):
    guard = "G4"


class StaleEvidence(GuardViolation):
    guard = "G5"


class RobotsDisallowed(GuardViolation):
    guard = "G6"


class AssistedOnly(GuardViolation):
    guard = "G7"


class SecretFound(GuardViolation):
    guard = "G8"


# --------------------------------------------------------------------------- G1
def assert_live_origin(receipts: Iterable[dict[str, Any]], lead_ref: str = "") -> None:
    bad = [r for r in receipts if (r.get("data_origin") or "live") != "live"]
    if bad:
        origins = sorted({r["data_origin"] for r in bad})
        raise DataOriginViolation(f"{lead_ref}: {len(bad)} receipt(s) with data_origin in {origins}")


# --------------------------------------------------------------------------- G2
def assert_recipient_allowed(recipient: str, allowed: list[str]) -> None:
    r = (recipient or "").strip().casefold()
    ok = {a.strip().casefold() for a in allowed if a and "TODO" not in a}
    # plus-addressing (user+token@domain) maps to the base mailbox
    base = re.sub(r"\+[^@]*@", "@", r)
    if r not in ok and base not in ok:
        raise RecipientNotAllowed(f"recipient {recipient!r} is not in seed_test.allowed_recipients")


# --------------------------------------------------------------------------- G3
def assert_contact_provenance(handle: str, source_url: str | None, source_quote: str | None,
                              channel_type: str) -> None:
    if not source_url or not source_url.startswith(("http://", "https://")):
        raise ProvenanceMissing(f"{channel_type} {handle!r}: missing source_url")
    if not source_quote or not source_quote.strip():
        raise ProvenanceMissing(f"{channel_type} {handle!r}: missing source_quote")
    if channel_type == "email":
        from receipts.util.urls import deobfuscate

        h = handle.casefold()
        if h not in source_quote.casefold() and h not in deobfuscate(source_quote).casefold():
            raise ProvenanceMissing(f"email {handle!r} does not appear in its source_quote")


# --------------------------------------------------------------------------- G4
G4_KEYS = ["sender.name", "sender.role", "sender.postal_address", "offer_line"]


def _dig(obj: Any, dotted: str) -> Any:
    for part in dotted.split("."):
        obj = obj.get(part) if isinstance(obj, dict) else getattr(obj, part, None)
    return obj


def config_todos(settings: Any, keys: list[str] = G4_KEYS) -> list[str]:
    return [k for k in keys if "TODO" in str(_dig(settings, k) or "TODO")]


def yaml_todos(path: str | Path) -> list[str]:
    """Return dotted keys whose value contains TODO; a missing file counts as one TODO."""
    p = Path(path)
    if not p.exists():
        return [f"{p.name}: file missing"]
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    found: list[str] = []

    def walk(node: Any, prefix: str) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{prefix}.{k}" if prefix else str(k))
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{prefix}[{i}]")
        elif isinstance(node, str) and "TODO" in node:
            found.append(f"{p.name}:{prefix}")

    walk(data, "")
    return found


def assert_config_complete(settings: Any, extra_yaml: Iterable[str | Path] = ()) -> None:
    missing = config_todos(settings)
    for y in extra_yaml:
        missing += yaml_todos(y)
    if missing:
        raise ConfigIncomplete("fill these before export (they still contain TODO): " + ", ".join(missing))


# --------------------------------------------------------------------------- G5
def assert_fresh(checked_at: Iterable[str | datetime], max_age_days: int, now: datetime,
                 lead_ref: str = "") -> None:
    cutoff = now - timedelta(days=max_age_days)
    for c in checked_at:
        dt = c if isinstance(c, datetime) else datetime.fromisoformat(str(c).replace("Z", "+00:00"))
        if dt < cutoff:
            raise StaleEvidence(f"{lead_ref}: evidence checked_at {dt.isoformat()} older than {max_age_days}d")


# --------------------------------------------------------------------------- G6
def robots_allows(robots_txt: str | None, url: str, user_agent: str) -> bool:
    if robots_txt is None:  # no robots.txt (404) means allowed
        return True
    rp = robotparser.RobotFileParser()
    rp.parse(robots_txt.splitlines())
    return rp.can_fetch(user_agent, url)


def assert_robots_allowed(robots_txt: str | None, url: str, user_agent: str) -> None:
    if not robots_allows(robots_txt, url, user_agent):
        raise RobotsDisallowed(f"robots.txt disallows {url} for {user_agent}")


# --------------------------------------------------------------------------- G7
ASSISTED_ONLY = frozenset({"linkedin", "instagram", "x"})


def assisted_flag(channel: str, assisted_types: Iterable[str] = ("linkedin", "instagram", "x", "form")) -> bool:
    return channel in set(assisted_types) | ASSISTED_ONLY


def assert_auto_send_allowed(channel: str) -> None:
    if channel in ASSISTED_ONLY:
        raise AssistedOnly(f"channel {channel} is assisted-send only; the scheduler never auto-sends it")


# --------------------------------------------------------------------------- G8
SECRET_PATTERNS = {
    "google_api_key": re.compile(r"AIza[0-9A-Za-z_\-]{35}"),
    "anthropic_key": re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    "openai_style_key": re.compile(r"\bsk-[A-Za-z0-9]{32,}\b"),
    "github_token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    "aws_access_key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "slack_token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
}
_SKIP_DIRS = {".git", ".venv", "out", "__pycache__", ".pytest_cache", ".ruff_cache", "node_modules"}
_BINARY_EXT = {".png", ".jpg", ".jpeg", ".gif", ".xlsx", ".pdf", ".db", ".pyc", ".mp4", ".zip", ".ico"}


def tracked_files(root: str | Path) -> list[Path]:
    """Files tracked by git; falls back to a walk that honours .gitignore basics when not a repo."""
    root = Path(root)
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True)
        names = [n for n in out.stdout.decode().split("\0") if n]
        if names:
            return [root / n for n in names]
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    ignore = []
    gi = root / ".gitignore"
    if gi.exists():
        ignore = [ln.strip().rstrip("/") for ln in gi.read_text().splitlines() if ln.strip() and not ln.startswith("#")]
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS
                       and not any(fnmatch.fnmatch(d, pat) for pat in ignore)]
        for f in filenames:
            if any(fnmatch.fnmatch(f, pat) for pat in ignore):
                continue
            files.append(Path(dirpath) / f)
    return files


def env_secret_values(env_path: str | Path, min_len: int = 8) -> list[str]:
    p = Path(env_path)
    if not p.exists():
        return []
    vals = []
    for line in p.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            v = v.strip().strip('"').strip("'")
            # non-secret config values (hosts, ports, flags) are skipped
            if len(v) >= min_len and not k.strip().endswith(("_HOST", "_PORT", "_MODE")):
                vals.append(v)
    return vals


def scan_for_secrets(files: Iterable[Path], env_values: list[str] = ()) -> list[tuple[str, str]]:
    findings = []
    for f in files:
        if f.name == ".env":
            findings.append((str(f), "env_file_tracked"))
            continue
        if f.suffix.lower() in _BINARY_EXT or not f.is_file():
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for name, pat in SECRET_PATTERNS.items():
            if pat.search(text):
                findings.append((str(f), name))
        for v in env_values:
            if v in text:
                findings.append((str(f), "env_value"))
    return findings
