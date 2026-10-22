"""Run context shared by all pipeline stages."""
from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from receipts.cache import SnapshotStore
from receipts.config import Settings, get_settings
from receipts.db import DB, init_db
from receipts.http import HttpClient
from receipts.sources.podcastindex import PodcastIndexAPI
from receipts.sources.youtube import YouTubeAPI
from receipts.util.dates import utcnow
from receipts.util.logging import NULL_LOGGER, JsonlLogger


@dataclass
class Ctx:
    settings: Settings
    db: DB
    store: SnapshotStore
    http: HttpClient
    yt: YouTubeAPI
    pi: PodcastIndexAPI
    llm: Any = None
    log: JsonlLogger = NULL_LOGGER
    run_id: int | None = None
    now: datetime = field(default_factory=utcnow)
    counters: dict[str, int] = field(default_factory=dict)

    def bump(self, k: str, n: int = 1) -> None:
        self.counters[k] = self.counters.get(k, 0) + n


@asynccontextmanager
async def open_context(settings: Settings | None = None, *, db: DB | None = None, fixture_mode: bool | None = None,
                       fixtures_dir: str | None = None, llm: Any = "auto", transport=None):
    s = settings or get_settings()
    own_db = db is None
    db = db or init_db(s.db_path)
    store = SnapshotStore(db, s.snapshots_dir)
    async with HttpClient(s, store, fixture_mode=fixture_mode, fixtures_dir=fixtures_dir, transport=transport) as http:
        if llm == "auto":
            from receipts.llm.client import make_llm

            llm = make_llm(s, db, fixture_mode=http.fixture_mode)
        ctx = Ctx(settings=s, db=db, store=store, http=http, yt=YouTubeAPI(http, db, s), pi=PodcastIndexAPI(http),
                  llm=llm)
        try:
            yield ctx
        finally:
            if own_db:
                db.close()
