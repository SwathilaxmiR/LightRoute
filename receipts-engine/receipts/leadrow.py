"""Build the leads.csv row (BUILD_SPEC 9.1) for one lead from stored state."""
from __future__ import annotations

from receipts.checks.c3_commercial import CLASSES
from receipts.checks.c4_needs import SIGNALS, strength
from receipts.db import DB
from receipts.models import CHANNEL_DISPLAY, PLATFORM_DISPLAY, SEGMENT_DISPLAY, Channel, Segment
from receipts.outreach.hooks import parse_metric
from receipts.pipeline.s8_messages import message_channel
from receipts.util.dates import parse_dt
from receipts.util.urls import registrable_domain

COMMERCIAL_ORDER = ["sponsorship", "product_or_course", "service", "membership", "affiliate"]
assert set(COMMERCIAL_ORDER) == set(CLASSES)


def monetization_phrase(r) -> str:
    cls, url, q = r["sub_key"], r["evidence_url"] or "", r["quote_or_metric"] or ""
    dom = registrable_domain(url) if url else ""
    if cls == "sponsorship":
        if "paidProductPlacement" in q:
            return "paid product placement flag on a recent video"
        if "youtube.com/watch" in url or "/ep" in url:
            return "sponsor read in a recent episode or video description"
        return "sponsorship or media kit page on own site"
    if cls == "product_or_course":
        return "course or product offer linked from own site" if dom else "course or product offer"
    if cls == "service":
        return "coaching, consulting or booking offer on own site"
    if cls == "membership":
        return "paid membership or supporter link"
    return "discount code or affiliate links in descriptions"


def need_sentence(sub: str, m: dict) -> str:
    base = sub.replace("_stated", "")
    if sub.endswith("_stated"):
        return f"creator states a {base.replace('_', ' ')} need on their own site"
    if base == "no_shortform":
        return "No short-form clips in 60 days"
    if base == "captions_gap":
        what = "transcripts" if "transcript" in m.get("field", "") else "captions"
        return f"{what} missing on {m.get('missing', '?')} of {m.get('sampled', '?')} recent long-form items"
    if base == "backlog":
        n = m.get("public_videos") or m.get("episodes")
        return f"{n} published items with no clips or transcripts drawn from them"
    if base == "inconsistent_publishing":
        return f"uneven publishing: longest gap {m.get('max_gap_days')} days vs median {m.get('median_gap_days')}"
    if base == "team_workflow":
        return "credits an editor or producer, so production is already a team workflow"
    if base == "single_platform":
        return "publishes on one platform with no other channels linked"
    return base


def ordered_needs(rows, s) -> list:
    return sorted(rows, key=lambda r: (strength(r["sub_key"], s) != "strong",
                                       SIGNALS.index(r["sub_key"].replace("_stated", "")) if r["sub_key"].replace("_stated", "") in SIGNALS else 99))


def profile_url(db: DB, cr) -> str:
    plat, pid = cr["primary_platform"], cr["primary_id"] or ""
    if plat == "youtube":
        return f"https://www.youtube.com/channel/{pid}"
    if plat == "podcast" and pid.startswith("pi:"):
        return f"https://podcastindex.org/podcast/{pid[3:]}"
    return cr["profile_url"] or ""


def build_row(db: DB, s, lead) -> dict:
    key = lead["creator_key"]
    cr = db.fetch_one("SELECT * FROM creators WHERE creator_key=?", (key,))
    rec = db.fetch_all("SELECT * FROM receipts WHERE creator_key=?", (key,))
    act = parse_metric(next((r["quote_or_metric"] for r in rec if r["check_name"] == "active"), ""))
    com = sorted([r for r in rec if r["check_name"] == "commercial" and r["sub_key"] and r["result"] == "pass"],
                 key=lambda r: COMMERCIAL_ORDER.index(r["sub_key"]) if r["sub_key"] in COMMERCIAL_ORDER else 9)
    needs = ordered_needs([r for r in rec if r["check_name"] == "needs" and r["sub_key"] and r["result"] == "pass"], s)
    best = db.fetch_one("SELECT * FROM channels WHERE creator_key=? AND role='best'", (key,))
    backup = db.fetch_one("SELECT * FROM channels WHERE creator_key=? AND role='backup'", (key,))
    latest = db.fetch_one("SELECT * FROM content_items WHERE creator_key=? ORDER BY published_at DESC LIMIT 1", (key,))
    recent_long = db.fetch_one("SELECT * FROM content_items WHERE creator_key=? AND duration_s >= ? "
                               "ORDER BY published_at DESC LIMIT 1", (key, s.thresholds.longform_min_seconds))
    msg = None
    if best:
        msg = db.fetch_one("SELECT * FROM messages WHERE creator_key=? AND channel=? AND step=1",
                           (key, message_channel(best["channel_type"])))
    first = ""
    if msg:
        first = (f"Subject: {msg['subject']}\n\n" if msg["subject"] else "") + msg["body"]
    seg = cr["segment"]
    checked = parse_dt(cr["last_verified"])
    return {
        "lead_id": lead["lead_id"],
        "creator_name": cr["display_name"],
        "creator_segment": SEGMENT_DISPLAY[Segment(seg)] if seg else "",
        "country_iso2": cr["country_iso2"] or "",
        "primary_language": cr["primary_language"] or "",
        "primary_platform": PLATFORM_DISPLAY.get(cr["primary_platform"] or "", ""),
        "profile_url": profile_url(db, cr),
        "stable_creator_id": cr["primary_id"] or "",
        "followers_or_subscribers": str(cr["followers"]) if cr["followers"] is not None else "hidden",
        "last_content_date": latest["published_at"][:10] if latest else "",
        "recent_content_url": (recent_long or latest)["url"] if (recent_long or latest) else "",
        "content_count_30d": act.get("count_30d", ""),
        "longform_count_60d": act.get("longform_60d", ""),
        "monetization_signal": "; ".join(f"{r['sub_key']}: {monetization_phrase(r)}" for r in com),
        "monetization_evidence_url": com[0]["evidence_url"] if com else "",
        "incubrix_need": "; ".join(need_sentence(r["sub_key"], parse_metric(r["quote_or_metric"])) for r in needs[:3]),
        "need_evidence_url": needs[0]["evidence_url"] if needs else "",
        "best_channel": CHANNEL_DISPLAY[Channel(best["channel_type"])] if best else "",
        "channel_handle_or_url": best["handle_or_url"] if best else "",
        "channel_evidence_url": best["source_url"] if best else "",
        "backup_channel": CHANNEL_DISPLAY[Channel(backup["channel_type"])] if backup else "",
        "personalization_hook": msg["hook"] if msg else "",
        "first_message": first,
        "checked_date": checked.strftime("%Y-%m-%d") if checked else "",
        "priority": lead["priority"],
        "lead_score": f"{lead['score']:.1f}",
        "candidate_status": "Qualified",
        "rejection_reason": "",
        "duplicate_check": "UNIQUE",
        "completeness_check": "COMPLETE",
        # not exported, used by checks/CRM
        "creator_key": key,
        "website_domain": cr["website_domain"] or "",
        "backup_value": backup["handle_or_url"] if backup else "",
        "best_type": best["channel_type"] if best else "",
        "backup_type": backup["channel_type"] if backup else "",
    }
