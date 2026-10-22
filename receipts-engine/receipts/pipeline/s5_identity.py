"""s5: identity, canonical creator_key, dedupe/merge.

Runs immediately after collection (before filtering) so duplicates merge into the earlier
creator_key and are not evaluated twice; merge semantics follow BUILD_SPEC 8.10 s5.
"""
from __future__ import annotations

import csv
import hashlib
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

from rapidfuzz import fuzz, process

from receipts.db import DB
from receipts.models import CollectedCreator
from receipts.util.dates import within_days
from receipts.util.urls import canonical_url, host_of, is_shared_host, normalise_handle, registrable_domain

SOCIAL_HOSTS = {"linkedin": ("linkedin.com",), "instagram": ("instagram.com",), "x": ("x.com", "twitter.com")}


def anchor(c: CollectedCreator, shared: list[str], blocklist: list[str] = ()) -> str:
    """youtube channel id > Podcast Index feed id > website domain > feed URL. Never a blocklisted domain."""
    yt = c.profile.get("youtube")
    if yt:
        return f"youtube:{yt['channel_id']}"
    fm = c.feed_meta or {}
    if fm.get("feed_id"):
        return f"podcastindex:{fm['feed_id']}"
    site = c.profile.get("website_url")
    if site and registrable_domain(site) not in blocklist:
        return f"web:{web_id(site, shared)}"
    if fm.get("url"):
        return f"rss:{canonical_url(fm['url'])}"
    return f"{c.candidate.platform}:{c.candidate.source_id}"


def web_id(url: str, shared: list[str]) -> str:
    """Registrable domain; on shared hosts the creator's own part: jane.substack.com, or medium.com/@jane
    for path-based hosts (so two creators on the same platform never share an identity)."""
    if not is_shared_host(url, shared):
        return registrable_domain(url)
    host = host_of(url)
    if host != registrable_domain(url):
        return host
    seg = next((p for p in urlsplit(url if "://" in url else "https://" + url).path.split("/") if p), "")
    return f"{host}/{seg.lower()}" if seg else host


def make_key(anchor_str: str) -> str:
    return "cr_" + hashlib.sha256(anchor_str.encode()).hexdigest()[:16]


def identities(c: CollectedCreator, s) -> list[dict]:
    shared = s.exclusions.get("shared_hosts", [])
    out: list[dict] = []
    yt = c.profile.get("youtube")
    if yt:
        out.append({"platform": "youtube", "platform_id": yt["channel_id"], "url": yt["url"], "source_snap_id": yt.get("snap_id")})
        if yt.get("custom_url"):
            h = normalise_handle("youtube", yt["custom_url"])
            out.append({"platform": "youtube", "platform_id": "@" + h, "url": f"https://www.youtube.com/@{h}",
                        "source_snap_id": yt.get("snap_id")})
    fm = c.feed_meta or {}
    if fm.get("feed_id"):
        out.append({"platform": "podcastindex", "platform_id": str(fm["feed_id"]),
                    "url": f"https://podcastindex.org/podcast/{fm['feed_id']}", "source_snap_id": fm.get("pi_snap_id")})
    if fm.get("itunes_id"):
        out.append({"platform": "itunes", "platform_id": str(fm["itunes_id"]), "url": None, "source_snap_id": None})
    if fm.get("url"):
        out.append({"platform": "rss", "platform_id": canonical_url(fm["url"]), "url": fm["url"],
                    "source_snap_id": fm.get("snap_id")})
    site = c.profile.get("website_url")
    if site and registrable_domain(site) not in s.exclusions.get("domains_blocklist", []):
        home_sid = c.website_pages.get(site, (None, ""))[0]
        out.append({"platform": "website", "platform_id": web_id(site, shared), "url": site, "source_snap_id": home_sid})
    # social profiles on the creator's own pages, only when the handle matches the creator's name
    name = c.profile.get("name", "")
    for link in c.profile.get("site_links", []):
        h = host_of(link)
        for plat, hosts in SOCIAL_HOSTS.items():
            if any(h == x or h.endswith("." + x) for x in hosts):
                handle = normalise_handle(plat, link)
                slug = handle.split("/")[-1]
                if slug and fuzz.partial_ratio(slug.replace("-", " "), name.lower()) >= s.identity.handle_name_match_ratio:
                    out.append({"platform": plat, "platform_id": handle, "url": link, "source_snap_id": None})
    seen, uniq = set(), []
    for r in out:
        k = (r["platform"], r["platform_id"])
        if k not in seen:
            seen.add(k)
            uniq.append(r)
    return uniq


def find_owner(db: DB, ids: list[dict], own_key: str, cand_id: int) -> str | None:
    """Earliest existing creator (by creators rowid) that already owns any of these identities, or own_key
    if a *different* candidate already created it."""
    owners = set()
    for r in ids:
        row = db.fetch_one("SELECT creator_key FROM identities WHERE platform=? AND platform_id=?",
                           (r["platform"], r["platform_id"]))
        if row:
            owners.add(row["creator_key"])
    if db.fetch_one("SELECT 1 FROM creators WHERE creator_key=?", (own_key,)):
        owners.add(own_key)
    if not owners:
        return None
    mine = db.scalar("SELECT creator_key FROM candidates WHERE cand_id=?", (cand_id,))
    others = [o for o in owners if o != mine]
    if not others:
        return None
    rows = db.fetch_all(f"SELECT creator_key, rowid FROM creators WHERE creator_key IN ({','.join('?' * len(others))}) "
                        "ORDER BY rowid", others)
    return rows[0]["creator_key"] if rows else None


def save_items(db: DB, key: str, c: CollectedCreator) -> None:
    for it in c.items:
        db.upsert("content_items", ["platform", "item_id"], {
            "creator_key": key, "platform": it.platform, "item_id": it.item_id, "url": it.url, "title": it.title,
            "published_at": it.published_at.isoformat(), "duration_s": it.duration_s, "is_short": int(it.is_short),
            "has_captions": None if it.has_captions is None else int(it.has_captions),
            "paid_placement": None if it.paid_placement is None else int(it.paid_placement),
            "description": it.description, "snap_id": it.snap_id})


def save_identities(db: DB, key: str, ids: list[dict]) -> None:
    for r in ids:
        db.execute("INSERT OR IGNORE INTO identities (creator_key, platform, platform_id, url, source_snap_id) "
                   "VALUES (?,?,?,?,?)", (key, r["platform"], r["platform_id"], r["url"], r["source_snap_id"]))


def merge_into(db: DB, target: str, c: CollectedCreator, ids: list[dict]) -> None:
    """Move this candidate's identities and content items to the earlier creator_key."""
    save_identities(db, target, ids)
    for it in c.items:
        db.execute("INSERT OR IGNORE INTO content_items (creator_key, platform, item_id, url, title, published_at, "
                   "duration_s, is_short, has_captions, paid_placement, description, snap_id) VALUES "
                   "(?,?,?,?,?,?,?,?,?,?,?,?)",
                   (target, it.platform, it.item_id, it.url, it.title, it.published_at.isoformat(), it.duration_s,
                    int(it.is_short), None if it.has_captions is None else int(it.has_captions),
                    None if it.paid_placement is None else int(it.paid_placement), it.description, it.snap_id))


def merge_creators(db: DB, keep: str, drop: str) -> None:
    """Merge two existing creators (used by refresh/repair): move identities, items, receipts; delete drop."""
    db.execute("UPDATE OR IGNORE identities SET creator_key=? WHERE creator_key=?", (keep, drop))
    db.execute("UPDATE OR IGNORE content_items SET creator_key=? WHERE creator_key=?", (keep, drop))
    db.execute("UPDATE OR IGNORE receipts SET creator_key=? WHERE creator_key=?", (keep, drop))
    db.execute("UPDATE candidates SET creator_key=? WHERE creator_key=?", (keep, drop))
    db.execute("DELETE FROM creators WHERE creator_key=?", (drop,))


def upsert_creator(db: DB, key: str, c: CollectedCreator, s, now) -> None:
    shared = s.exclusions.get("shared_hosts", [])
    yt = c.profile.get("youtube") or {}
    site = c.profile.get("website_url")
    row = {"creator_key": key, "display_name": c.profile.get("name") or key,
           "website_domain": web_id(site, shared) if site else None,
           "followers": c.profile.get("followers"), "last_verified": now.isoformat(),
           "profile_url": yt.get("url") or (c.feed_meta or {}).get("link") or site or c.candidate.url}
    if db.fetch_one("SELECT 1 FROM creators WHERE creator_key=?", (key,)):
        sets = ", ".join(f"{k}=:{k}" for k in row if k != "creator_key")
        db.execute(f"UPDATE creators SET {sets}, status='open', reject_reason=NULL WHERE creator_key=:creator_key", row)
    else:
        db.insert("creators", row)


def primary_platform(c: CollectedCreator, segment: str, now, window_days: int) -> tuple[str, str]:
    """('youtube'|'podcast'|'website', stable id). More items in the window wins; tie per segment."""
    n_yt = sum(1 for i in c.items if i.platform == "youtube" and within_days(i.published_at, window_days, now))
    n_pod = sum(1 for i in c.items if i.platform == "podcast" and within_days(i.published_at, window_days, now))
    yt = c.profile.get("youtube")
    fm = c.feed_meta or {}
    if yt and (n_yt > n_pod or (n_yt == n_pod and segment != "podcaster") or not c.feed_meta):
        return "youtube", yt["channel_id"]
    if c.feed_meta:
        if fm.get("feed_id"):
            return "podcast", f"pi:{fm['feed_id']}"
        return "podcast", f"rss:{canonical_url(fm['url'])}"
    site = c.profile.get("website_url") or c.candidate.url
    return "website", f"web:{registrable_domain(site)}"


def review_fuzzy(db: DB, key: str, name: str, out_dir: Path, ratio: float) -> int:
    """Write likely-duplicate names to review_possible_dupes.csv; never merges."""
    rows = db.fetch_all("SELECT creator_key, display_name FROM creators WHERE creator_key != ?", (key,))
    if not rows or not name:
        return 0
    choices = {r["creator_key"]: r["display_name"] for r in rows}
    hits = process.extract(name, choices, scorer=fuzz.token_set_ratio, score_cutoff=ratio, limit=5)
    if not hits:
        return 0
    path = out_dir / "logs" / "review_possible_dupes.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["creator_key", "name", "possible_dupe_key", "possible_dupe_name", "score"])
        for other_name, score, other_key in hits:
            w.writerow([key, name, other_key, other_name, round(score, 1)])
    return len(hits)


def ttl_due(last_verified: str | None, now, ttl_days: int) -> bool:
    from receipts.util.dates import parse_dt

    lv = parse_dt(last_verified)
    return lv is None or now - lv > timedelta(days=ttl_days)
