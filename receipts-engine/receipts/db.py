"""SQLite storage. WAL mode, foreign keys on. Schema from BUILD_SPEC section 6."""
from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);

CREATE TABLE IF NOT EXISTS candidates (
  cand_id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL,
  source_id TEXT NOT NULL,
  platform TEXT NOT NULL,
  url TEXT NOT NULL,
  first_seen TEXT NOT NULL,
  stage TEXT NOT NULL DEFAULT 'ingested',
  status TEXT NOT NULL DEFAULT 'open',
  reject_reason TEXT,
  run_id INTEGER,
  creator_key TEXT,
  hint TEXT,
  reject_detail TEXT,
  rejected_at TEXT,
  UNIQUE(platform, source_id)
);

CREATE TABLE IF NOT EXISTS snapshots (
  snap_id INTEGER PRIMARY KEY AUTOINCREMENT,
  url TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  status_code INTEGER,
  content_type TEXT,
  sha256 TEXT NOT NULL,
  path TEXT NOT NULL,
  data_origin TEXT NOT NULL DEFAULT 'live',
  api_units INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_snap_url ON snapshots(url);

CREATE TABLE IF NOT EXISTS creators (
  creator_key TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  segment TEXT,
  country_iso2 TEXT,
  primary_language TEXT,
  primary_platform TEXT,
  primary_id TEXT,
  profile_url TEXT,
  website_domain TEXT,
  followers INTEGER,
  last_verified TEXT,
  status TEXT NOT NULL DEFAULT 'open',
  reject_reason TEXT
);

CREATE TABLE IF NOT EXISTS identities (
  creator_key TEXT NOT NULL REFERENCES creators(creator_key) ON DELETE CASCADE,
  platform TEXT NOT NULL,
  platform_id TEXT NOT NULL,
  url TEXT,
  source_snap_id INTEGER REFERENCES snapshots(snap_id),
  PRIMARY KEY (platform, platform_id)
);

CREATE TABLE IF NOT EXISTS content_items (
  creator_key TEXT NOT NULL,
  platform TEXT NOT NULL,
  item_id TEXT NOT NULL,
  url TEXT NOT NULL,
  title TEXT,
  published_at TEXT NOT NULL,
  duration_s INTEGER,
  is_short INTEGER DEFAULT 0,
  has_captions INTEGER,
  paid_placement INTEGER,
  description TEXT,
  snap_id INTEGER REFERENCES snapshots(snap_id),
  PRIMARY KEY (platform, item_id)
);
CREATE INDEX IF NOT EXISTS ix_items_creator ON content_items(creator_key);

CREATE TABLE IF NOT EXISTS receipts (
  receipt_id INTEGER PRIMARY KEY AUTOINCREMENT,
  creator_key TEXT NOT NULL REFERENCES creators(creator_key) ON DELETE CASCADE,
  check_name TEXT NOT NULL,
  sub_key TEXT NOT NULL DEFAULT '',
  result TEXT NOT NULL,
  evidence_url TEXT,
  quote_or_metric TEXT,
  snap_id INTEGER REFERENCES snapshots(snap_id),
  data_origin TEXT NOT NULL DEFAULT 'live',
  verified_by TEXT NOT NULL,
  checked_at TEXT NOT NULL,
  reason_code TEXT,
  UNIQUE(creator_key, check_name, sub_key)
);

CREATE TABLE IF NOT EXISTS channels (
  channel_id INTEGER PRIMARY KEY AUTOINCREMENT,
  creator_key TEXT NOT NULL REFERENCES creators(creator_key) ON DELETE CASCADE,
  channel_type TEXT NOT NULL,
  handle_or_url TEXT NOT NULL,
  source_url TEXT NOT NULL CHECK (length(source_url) > 0),
  source_quote TEXT NOT NULL CHECK (length(source_quote) > 0),
  snap_id INTEGER REFERENCES snapshots(snap_id),
  business_language INTEGER NOT NULL,
  live_ok INTEGER NOT NULL,
  name_match INTEGER NOT NULL,
  mx_ok INTEGER,
  last_active TEXT,
  active_proof TEXT,
  manual_check_path TEXT,
  channel_score REAL,
  role TEXT,
  assisted_send INTEGER NOT NULL DEFAULT 0,
  verified INTEGER NOT NULL DEFAULT 0,
  checked_at TEXT NOT NULL,
  UNIQUE(creator_key, channel_type, handle_or_url)
);

CREATE TABLE IF NOT EXISTS leads (
  lead_id TEXT PRIMARY KEY,
  creator_key TEXT NOT NULL UNIQUE REFERENCES creators(creator_key) ON DELETE CASCADE,
  score REAL NOT NULL,
  score_breakdown TEXT NOT NULL,
  priority TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'Qualified',
  first_exported_at TEXT,
  last_exported_at TEXT
);

CREATE TABLE IF NOT EXISTS messages (
  creator_key TEXT NOT NULL REFERENCES creators(creator_key) ON DELETE CASCADE,
  channel TEXT NOT NULL,
  step INTEGER NOT NULL,
  subject TEXT,
  body TEXT NOT NULL,
  hook TEXT NOT NULL,
  hook_source_url TEXT NOT NULL,
  validated INTEGER NOT NULL DEFAULT 0,
  validator_report TEXT,
  assisted_send INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  PRIMARY KEY (creator_key, channel, step)
);

CREATE TABLE IF NOT EXISTS runs (
  run_id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  started TEXT NOT NULL,
  ended TEXT,
  input_desc TEXT,
  input_size INTEGER DEFAULT 0,
  produced INTEGER DEFAULT 0,
  rejected INTEGER DEFAULT 0,
  dupes_removed INTEGER DEFAULT 0,
  unchanged INTEGER DEFAULT 0,
  updated INTEGER DEFAULT 0,
  youtube_units INTEGER DEFAULT 0,
  llm_in_tokens INTEGER DEFAULT 0,
  llm_out_tokens INTEGER DEFAULT 0,
  llm_cost_usd REAL DEFAULT 0,
  direct_cost_usd REAL DEFAULT 0,
  status TEXT DEFAULT 'running',
  log_path TEXT,
  note TEXT,
  data_origin TEXT
);

CREATE TABLE IF NOT EXISTS llm_cache (
  key TEXT PRIMARY KEY,
  response TEXT NOT NULL,
  in_tokens INTEGER, out_tokens INTEGER,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schedule (
  sched_id INTEGER PRIMARY KEY AUTOINCREMENT,
  lead_id TEXT NOT NULL, channel TEXT NOT NULL, step INTEGER NOT NULL,
  due_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
  sent_at TEXT, token TEXT NOT NULL UNIQUE, recipient TEXT NOT NULL,
  UNIQUE(lead_id, channel, step)
);
CREATE TABLE IF NOT EXISTS clicks (
  click_id INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT NOT NULL, at TEXT NOT NULL, ua_hash TEXT
);
CREATE TABLE IF NOT EXISTS replies (
  reply_id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id TEXT NOT NULL, channel TEXT NOT NULL,
  received_at TEXT NOT NULL, label TEXT NOT NULL,
  excerpt TEXT, source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS form_submissions (
  sub_id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id TEXT, token TEXT, message TEXT, at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS suppressions (
  lead_id TEXT PRIMARY KEY, reason TEXT NOT NULL, at TEXT NOT NULL
);
"""


class DB:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._depth = 0

    def close(self) -> None:
        self.conn.close()

    # ---- transactions -----------------------------------------------------
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Nestable transaction; outermost level commits or rolls back."""
        outer = self._depth == 0
        if outer:
            self.conn.execute("BEGIN IMMEDIATE")
        self._depth += 1
        try:
            yield self.conn
        except BaseException:
            self._depth -= 1
            if outer:
                self.conn.execute("ROLLBACK")
            raise
        else:
            self._depth -= 1
            if outer:
                self.conn.execute("COMMIT")

    # ---- helpers ----------------------------------------------------------
    def execute(self, sql: str, params: Iterable[Any] | dict = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def fetch_one(self, sql: str, params: Iterable[Any] | dict = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def fetch_all(self, sql: str, params: Iterable[Any] | dict = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def scalar(self, sql: str, params: Iterable[Any] | dict = ()) -> Any:
        row = self.conn.execute(sql, params).fetchone()
        return None if row is None else row[0]

    def upsert(self, table: str, key_cols: list[str], row: dict[str, Any]) -> None:
        """INSERT ... ON CONFLICT(key_cols) DO UPDATE SET non-key columns."""
        cols = list(row)
        placeholders = ", ".join(f":{c}" for c in cols)
        updates = [c for c in cols if c not in key_cols]
        sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})"
        if updates:
            sets = ", ".join(f"{c}=excluded.{c}" for c in updates)
            sql += f" ON CONFLICT({', '.join(key_cols)}) DO UPDATE SET {sets}"
        else:
            sql += f" ON CONFLICT({', '.join(key_cols)}) DO NOTHING"
        self.conn.execute(sql, row)

    def insert(self, table: str, row: dict[str, Any], or_ignore: bool = False) -> int:
        cols = list(row)
        verb = "INSERT OR IGNORE" if or_ignore else "INSERT"
        cur = self.conn.execute(
            f"{verb} INTO {table} ({', '.join(cols)}) VALUES ({', '.join(':' + c for c in cols)})", row
        )
        return cur.lastrowid if cur.rowcount else 0

    # ---- meta -------------------------------------------------------------
    def meta_get(self, k: str, default: str | None = None) -> str | None:
        v = self.scalar("SELECT v FROM meta WHERE k=?", (k,))
        return default if v is None else v

    def meta_set(self, k: str, v: str) -> None:
        self.upsert("meta", ["k"], {"k": k, "v": v})


# columns added after schema v1 shipped: (table, column, type)
MIGRATIONS = [("candidates", "creator_key", "TEXT"), ("candidates", "hint", "TEXT"),
              ("candidates", "reject_detail", "TEXT"), ("candidates", "rejected_at", "TEXT"),
              ("runs", "data_origin", "TEXT")]


def init_db(path: str | Path) -> DB:
    db = DB(path)
    db.conn.executescript(SCHEMA)
    for table, col, typ in MIGRATIONS:
        cols = {r["name"] for r in db.fetch_all(f"PRAGMA table_info({table})")}
        if col not in cols:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    db.meta_set("schema_version", SCHEMA_VERSION)
    return db
