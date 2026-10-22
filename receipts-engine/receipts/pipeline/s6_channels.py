"""s6: contact channels (check c5). Manual-check data recorded by `receipts manual-check` survives re-runs."""
from __future__ import annotations

from receipts.checks.c5_reachable import (
    CONTACT_PATH,
    Route,
    extract_routes,
    reachable_result,
    responsiveness_points,
    score_routes,
    select,
    verify_routes,
)
from receipts.pipeline.common import Reject, save_receipt
from receipts.pipeline.runner import register_stage
from receipts.sources.dns import mx_hosts
from receipts.sources.youtube import YouTubeError
from receipts.util.dates import utcnow


def manual_checks(db, key: str) -> dict[tuple[str, str], dict]:
    rows = db.fetch_all("SELECT channel_type, handle_or_url, manual_check_path, last_active FROM channels "
                        "WHERE creator_key=? AND active_proof='manual'", (key,))
    return {(r["channel_type"], r["handle_or_url"]): dict(r) for r in rows}


def save_routes(db, key: str, routes: list[Route]) -> None:
    now = utcnow().isoformat()
    keep = []
    for r in routes:
        row = {"creator_key": key, "channel_type": r.channel_type, "handle_or_url": r.handle_or_url,
               "source_url": r.source_url, "source_quote": r.source_quote, "snap_id": r.snap_id,
               "business_language": int(r.business_language), "live_ok": int(r.live_ok), "name_match": int(r.name_match),
               "mx_ok": None if r.mx_ok is None else int(r.mx_ok), "last_active": r.last_active,
               "active_proof": r.active_proof, "manual_check_path": r.manual_check_path,
               "channel_score": r.channel_score, "role": r.role, "assisted_send": int(r.assisted_send),
               "verified": int(r.verified), "checked_at": now}
        db.upsert("channels", ["creator_key", "channel_type", "handle_or_url"], row)
        keep.append((r.channel_type, r.handle_or_url))
    for row in db.fetch_all("SELECT channel_id, channel_type, handle_or_url FROM channels WHERE creator_key=?", (key,)):
        if (row["channel_type"], row["handle_or_url"]) not in keep:
            db.execute("DELETE FROM channels WHERE channel_id=?", (row["channel_id"],))


async def evaluate_channels(ctx, c, key: str) -> tuple[list[Route], Route | None, Route | None]:
    s = ctx.settings
    routes = extract_routes(c, ctx.store, s)
    mx: dict[str, bool | None] = {}
    for dom in {r.handle_or_url.split("@")[-1].lower() for r in routes if r.channel_type == "email"}:
        hosts = await mx_hosts(dom, fixture_mode=ctx.http.fixture_mode, fixtures_dir=s.fixtures_dir)
        mx[dom] = None if hosts is None else bool(hosts)
    verify_routes(routes, c, s, ctx.now, mx, manual_checks(ctx.db, key))
    reply_rate = None
    yt = c.profile.get("youtube")
    if yt and any(r.verified for r in routes):
        vids = [i.item_id for i in sorted(c.items, key=lambda i: i.published_at, reverse=True) if i.platform == "youtube"]
        try:
            reply_rate = await ctx.yt.comment_reply_rate(yt["channel_id"], vids)
        except YouTubeError:
            reply_rate = None
    guest_form = any(r.channel_type == "form" and CONTACT_PATH.search(r.source_url) and
                     any(w in r.source_url.lower() for w in ("guest", "sponsor", "pitch", "advertise")) for r in routes)
    score_routes(routes, c, s, responsiveness_points(reply_rate, guest_form, s))
    best, backup = select(routes)
    return routes, best, backup


@register_stage("s6")
async def channels_stage(ctx, c, key: str, fr, origin: str) -> None:
    routes, best, backup = await evaluate_channels(ctx, c, key)
    res = reachable_result(best, backup, routes)
    with ctx.db.transaction():
        save_routes(ctx.db, key, routes)
        save_receipt(ctx.db, key, res, origin)
    if not res.passed:
        raise Reject(res.reason_code, stage="channelled")


def reselect(db, key: str) -> tuple[dict | None, dict | None]:
    """Re-run best/backup selection from stored channel rows (after a manual check)."""
    rows = [dict(r) for r in db.fetch_all("SELECT * FROM channels WHERE creator_key=?", (key,))]
    routes = []
    for r in rows:
        rt = Route(r["channel_type"], r["handle_or_url"], r["source_url"], r["source_quote"], r["snap_id"],
                   business_language=bool(r["business_language"]), verified=bool(r["verified"]),
                   channel_score=r["channel_score"] or 0)
        routes.append(rt)
    best, backup = select(routes)
    with db.transaction():
        db.execute("UPDATE channels SET role='candidate' WHERE creator_key=?", (key,))
        for rt in (best, backup):
            if rt:
                db.execute("UPDATE channels SET role=? WHERE creator_key=? AND channel_type=? AND handle_or_url=?",
                           (rt.role, key, rt.channel_type, rt.handle_or_url))

    def row_of(rt: Route | None) -> dict | None:
        if rt is None:
            return None
        return next((r for r in rows if r["channel_type"] == rt.channel_type and r["handle_or_url"] == rt.handle_or_url),
                    None)

    return row_of(best), row_of(backup)
