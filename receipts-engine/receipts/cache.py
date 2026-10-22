"""Snapshot store: response bodies on disk under <snapshots_dir>/<sha[:2]>/<sha>, rows in `snapshots`.

load_snapshot_text returns a *text view* that quotes are verified against:
- JSON: pretty-printed JSON plus every string leaf on its own line (so multi-line
  descriptions are searchable without JSON escaping).
- HTML: visible text plus every href (so mailto: and link targets are quotable).
- XML/RSS: unescaped raw XML plus tag-stripped text.
"""
from __future__ import annotations

import hashlib
import html as htmllib
import json
import re
from datetime import timedelta
from pathlib import Path

from receipts.db import DB
from receipts.util.dates import parse_dt, utcnow
from receipts.util.text import visible_text


class SnapshotStore:
    def __init__(self, db: DB, root: str | Path):
        self.db = db
        self.root = Path(root)
        self._text_cache: dict[int, str] = {}

    def save_snapshot(self, url: str, body: bytes, status: int | None, content_type: str | None,
                      data_origin: str = "live", api_units: int = 0) -> int:
        sha = hashlib.sha256(body).hexdigest()
        path = self.root / sha[:2] / sha
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        now = utcnow().isoformat()
        existing = self.db.fetch_one(
            "SELECT snap_id FROM snapshots WHERE url=? AND sha256=? AND data_origin=? ORDER BY snap_id DESC LIMIT 1",
            (url, sha, data_origin),
        )
        if existing:
            self.db.execute("UPDATE snapshots SET fetched_at=? WHERE snap_id=?", (now, existing["snap_id"]))
            return existing["snap_id"]
        return self.db.insert("snapshots", {
            "url": url, "fetched_at": now, "status_code": status, "content_type": content_type or "",
            "sha256": sha, "path": str(path), "data_origin": data_origin, "api_units": api_units,
        })

    def get(self, snap_id: int):
        return self.db.fetch_one("SELECT * FROM snapshots WHERE snap_id=?", (snap_id,))

    def load_raw(self, snap_id: int) -> bytes:
        row = self.get(snap_id)
        if row is None:
            raise KeyError(f"snapshot {snap_id} not found")
        return Path(row["path"]).read_bytes()

    def load_snapshot_text(self, snap_id: int) -> str:
        if snap_id in self._text_cache:
            return self._text_cache[snap_id]
        row = self.get(snap_id)
        if row is None:
            return ""
        raw = Path(row["path"]).read_bytes().decode("utf-8", errors="replace")
        ctype = (row["content_type"] or "").lower()
        text = text_view(raw, ctype)
        self._text_cache[snap_id] = text
        return text

    def fresh_snapshot(self, url: str, max_age_hours: float) -> int | None:
        row = self.db.fetch_one(
            "SELECT snap_id, fetched_at FROM snapshots WHERE url=? ORDER BY fetched_at DESC LIMIT 1", (url,)
        )
        if not row:
            return None
        fetched = parse_dt(row["fetched_at"])
        if fetched and utcnow() - fetched <= timedelta(hours=max_age_hours):
            return row["snap_id"]
        return None


def _string_leaves(node, out: list[str]) -> None:
    if isinstance(node, dict):
        for v in node.values():
            _string_leaves(v, out)
    elif isinstance(node, list):
        for v in node:
            _string_leaves(v, out)
    elif isinstance(node, str):
        out.append(node)


_TAG = re.compile(r"<[^>]+>")


def text_view(raw: str, content_type: str = "") -> str:
    stripped = raw.lstrip()
    if "json" in content_type or stripped[:1] in "{[":
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        if data is not None:
            leaves: list[str] = []
            _string_leaves(data, leaves)
            return json.dumps(data, indent=2, ensure_ascii=False) + "\n" + "\n".join(leaves)
    if "xml" in content_type or "rss" in content_type or stripped.startswith("<?xml"):
        un = htmllib.unescape(raw)
        return un + "\n" + htmllib.unescape(_TAG.sub(" ", un))
    if "html" in content_type or "<html" in stripped[:500].lower() or "<body" in raw[:5000].lower():
        from selectolax.lexbor import LexborHTMLParser as HTMLParser

        hrefs = [a.attributes.get("href") or "" for a in HTMLParser(raw).css("a[href]")]
        return visible_text(raw) + "\n" + "\n".join(h for h in hrefs if h)
    return raw
