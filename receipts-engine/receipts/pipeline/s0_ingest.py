"""s0: parse an input file (.yaml seeds, .csv, .txt) or keywords into candidates."""
from __future__ import annotations

import csv
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from receipts.db import DB
from receipts.models import Candidate
from receipts.util.dates import utcnow
from receipts.util.urls import canonical_url, host_of, youtube_channel_url

HEADER_NAMES = {"url", "handle", "channel_id", "feed_url", "rss", "website", "name", "channel"}
VALUE_COLUMNS = ["url", "handle", "channel_id", "feed_url", "rss", "website", "channel"]

CHANNEL_ID = re.compile(r"\b(UC[0-9A-Za-z_-]{22})\b")
YT_CHANNEL_URL = re.compile(r"youtube\.com/channel/(UC[0-9A-Za-z_-]{22})", re.I)
YT_HANDLE_URL = re.compile(r"youtube\.com/(?:@|c/|user/)([A-Za-z0-9._-]+)", re.I)
FEED_URL = re.compile(r"(\.xml\b|/feed\b|rss)", re.I)
PI_ID = re.compile(r"^pi:(\d+)$", re.I)
AT_HANDLE = re.compile(r"^@([A-Za-z0-9._-]{3,30})$")


@dataclass
class InputResult:
    candidates: list[Candidate] = field(default_factory=list)
    rejects: list[str] = field(default_factory=list)
    is_seeds: bool = False
    keywords: list[str] = field(default_factory=list)


def classify(cell: str, shared_hosts: list[str]) -> Candidate | None:
    v = (cell or "").strip().strip('"').strip()
    if not v:
        return None
    if m := PI_ID.match(v):
        return Candidate(platform="podcast", source_id=m.group(1), url=f"https://podcastindex.org/podcast/{m.group(1)}",
                         source="input_file")
    if m := YT_CHANNEL_URL.search(v):
        return Candidate(platform="youtube", source_id=m.group(1), url=youtube_channel_url(m.group(1)), source="input_file")
    if m := YT_HANDLE_URL.search(v):
        h = m.group(1).lower()
        return Candidate(platform="youtube", source_id="@" + h, url=f"https://www.youtube.com/@{h}", source="input_file",
                         hint={"handle": h})
    if m := CHANNEL_ID.fullmatch(v):
        return Candidate(platform="youtube", source_id=m.group(1), url=youtube_channel_url(m.group(1)), source="input_file")
    if m := AT_HANDLE.match(v):
        h = m.group(1).lower()
        return Candidate(platform="youtube", source_id="@" + h, url=f"https://www.youtube.com/@{h}", source="input_file",
                         hint={"handle": h})
    if v.lower().startswith(("http://", "https://")) or re.match(r"^[\w-]+(\.[\w-]+)+(/\S*)?$", v):
        url = v if "://" in v else "https://" + v
        if host_of(url) in ("youtube.com", "m.youtube.com", "youtu.be"):
            return None  # a YouTube URL we cannot map to a channel (video link, playlist): unparseable
        if FEED_URL.search(url.split("://", 1)[1]):
            sid = "rss:" + hashlib.sha256(canonical_url(url).encode()).hexdigest()[:16]
            return Candidate(platform="podcast", source_id=sid, url=url, source="input_file", hint={"feed_url": url})
        from receipts.pipeline.s5_identity import web_id

        return Candidate(platform="website", source_id="web:" + web_id(url, shared_hosts), url=url, source="input_file")
    return None


def load_input(spec: str | Path | None, keywords: list[str] | None, shared_hosts: list[str]) -> InputResult:
    res = InputResult(keywords=[k.strip() for k in (keywords or []) if k.strip()])
    if spec is None or str(spec) == "":
        return res
    p = Path(spec)
    suffix = p.suffix.lower()
    if suffix in (".yaml", ".yml"):
        res.is_seeds = True
        return res
    if not p.exists():
        # free-text keywords given via --input "a,b"
        res.keywords += [k.strip() for k in str(spec).split(",") if k.strip()]
        return res
    cells: list[tuple[str, str]] = []  # (value to classify, original row text)
    if suffix == ".csv":
        with p.open(encoding="utf-8-sig", newline="") as f:
            rows = list(csv.reader(f))
        if rows:
            header = [h.strip().lower() for h in rows[0]]
            cols = [i for i, h in enumerate(header) if h in VALUE_COLUMNS]
            if any(h in HEADER_NAMES for h in header):
                for r in rows[1:]:
                    vals = [r[i].strip() for i in cols if i < len(r) and r[i].strip()]
                    if vals:
                        cells.append((vals[0], ",".join(r)))
                    elif any(c.strip() for c in r):
                        cells.append((",".join(r), ",".join(r)))
            else:
                cells = [(r[0], ",".join(r)) for r in rows if r and r[0].strip()]
    else:
        cells = [(ln.strip(), ln.strip()) for ln in p.read_text(encoding="utf-8").splitlines()
                 if ln.strip() and not ln.startswith("#")]

    seen: set[tuple[str, str]] = set()
    for value, original in cells:
        cand = classify(value, shared_hosts)
        if cand is None:
            res.rejects.append(original)
            continue
        k = (cand.platform, cand.source_id)
        if k not in seen:
            seen.add(k)
            res.candidates.append(cand)
    return res


def insert_candidates(db: DB, cands: list[Candidate], run_id: int | None, stage: str = "discovered") -> int:
    """INSERT OR IGNORE by (platform, source_id). Returns number of new rows."""
    new = 0
    now = utcnow().isoformat()
    with db.transaction():
        for c in cands:
            if db.insert("candidates", {"source": c.source, "source_id": c.source_id, "platform": c.platform,
                                        "url": c.url, "first_seen": now, "stage": stage, "status": "open",
                                        "run_id": run_id, "hint": json.dumps(c.hint)}, or_ignore=True):
                new += 1
    return new


def insert_input_rejects(db: DB, raw: list[str], run_id: int | None) -> None:
    now = utcnow().isoformat()
    with db.transaction():
        for text in raw:
            db.insert("candidates", {"source": "input_file", "source_id": "raw:" + hashlib.sha256(text.encode()).hexdigest()[:16],
                                     "platform": "input", "url": text[:500], "first_seen": now, "stage": "ingested",
                                     "status": "rejected", "reject_reason": "unparseable_input", "reject_detail": text[:300],
                                     "rejected_at": now, "run_id": run_id},
                      or_ignore=True)


def row_to_candidate(row) -> Candidate:
    return Candidate(platform=row["platform"], source_id=row["source_id"], url=row["url"], source=row["source"],
                     hint=json.loads(row["hint"] or "{}"))
