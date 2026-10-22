"""s1: discovery. YouTube channel search + Podcast Index search, resume-safe via meta keys."""
from __future__ import annotations

import json
from datetime import timedelta

from receipts.http import FixtureMissing
from receipts.models import Candidate
from receipts.pipeline.s0_ingest import insert_candidates
from receipts.util.urls import youtube_channel_url

DONE_KEY = "discovery_queries"


def youtube_queries(seeds: dict, extra: list[str] | None = None) -> list[str]:
    out: list[str] = list(extra or [])
    for tpl in seeds.get("youtube_search_templates", []):
        for niche in seeds.get("niches", []):
            if "{intent}" in tpl:
                out += [tpl.format(niche=niche, intent=i) for i in seeds.get("intents", [])]
            else:
                out.append(tpl.format(niche=niche))
    return list(dict.fromkeys(out))


def podcast_terms(seeds: dict, extra: list[str] | None = None) -> list[tuple[str, str]]:
    terms = [("term", k) for k in (extra or [])]
    terms += [("term", f"{n} podcast") for n in seeds.get("niches", [])]
    terms += [("category", c) for c in seeds.get("podcast_categories", [])]
    return list(dict.fromkeys(terms))


def _done(db) -> set[str]:
    return set(json.loads(db.meta_get(DONE_KEY, "[]") or "[]"))


def _mark(db, key: str) -> None:
    done = _done(db)
    done.add(key)
    db.meta_set(DONE_KEY, json.dumps(sorted(done)))


async def discover(ctx, *, keywords: list[str] | None = None, max_queries: int | None = None,
                   youtube: bool = True, podcasts: bool = True) -> int:
    """Run unfinished queries. Stops cleanly when the quota ledger cannot afford another search.
    Returns number of new candidates. QuotaExceeded propagates to the runner."""
    s, db = ctx.settings, ctx.db
    seeds = s.seeds
    per = int(seeds.get("max_results_per_query", 50))
    new = 0
    ran = 0
    done = _done(db)

    if youtube:
        for q in youtube_queries(seeds, keywords):
            key = f"yt:{q}"
            if key in done:
                continue
            if max_queries is not None and ran >= max_queries:
                break
            if not ctx.http.fixture_mode and ctx.yt.ledger.remaining < ctx.yt.ledger.cost("search_list"):
                ctx.log.log("discovery_quota_stop", remaining=ctx.yt.ledger.remaining)
                break
            try:
                ids, _ = await ctx.yt.search_channels(q, max_results=per)
            except FixtureMissing:
                ids = []
            cands = [Candidate(platform="youtube", source_id=i, url=youtube_channel_url(i), source="youtube_search",
                               hint={"query": q, "depth": 0}) for i in ids]
            new += insert_candidates(db, cands, ctx.run_id)
            _mark(db, key)
            ran += 1
            ctx.log.log("discovery_query", platform="youtube", q=q, found=len(ids))

    if podcasts:
        cutoff = ctx.now - timedelta(days=s.discovery.podcast_max_age_days)
        lang = s.discovery.podcast_language_prefix
        for kind, term in podcast_terms(seeds, keywords):
            key = f"pi:{kind}:{term}"
            if key in done:
                continue
            if max_queries is not None and ran >= max_queries:
                break
            try:
                feeds = await (ctx.pi.search_by_term(term, max=per) if kind == "term"
                               else ctx.pi.by_category(term, max=per, lang=lang))
            except FixtureMissing:
                feeds = []
            keep = [f for f in feeds if (f.language or "").lower().startswith(lang) and not f.dead
                    and (f.newest_item or f.last_update or 0) >= cutoff.timestamp()]
            cands = [Candidate(platform="podcast", source_id=str(f.feed_id),
                               url=f"https://podcastindex.org/podcast/{f.feed_id}", source="podcastindex",
                               hint={"query": term, "depth": 0}) for f in keep]
            new += insert_candidates(db, cands, ctx.run_id)
            _mark(db, key)
            ran += 1
            ctx.log.log("discovery_query", platform="podcast", q=term, found=len(feeds), kept=len(keep))
    return new


def snowball(ctx, creator_key: str, links: list[str], depth: int) -> int:
    """Add creator-linked YouTube channels as new candidates (max depth from settings)."""
    import re

    if depth >= ctx.settings.discovery.snowball_depth:
        return 0
    cands = []
    for u in links:
        m = re.search(r"youtube\.com/channel/(UC[0-9A-Za-z_-]{22})", u)
        if m:
            cands.append(Candidate(platform="youtube", source_id=m.group(1), url=youtube_channel_url(m.group(1)),
                                   source="link_snowball", hint={"from": creator_key, "depth": depth + 1}))
    return insert_candidates(ctx.db, cands, ctx.run_id) if cands else 0
