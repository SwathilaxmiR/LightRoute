"""s4: commercial (c3) and needs (c4) evidence, then one-hop link snowball."""
from __future__ import annotations

from receipts.checks.c3_commercial import check_commercial
from receipts.checks.c4_needs import check_needs
from receipts.pipeline.common import Reject, save_receipt
from receipts.pipeline.runner import register_stage
from receipts.pipeline.s1_discover import snowball


@register_stage("s4")
async def evidence_stage(ctx, c, key: str, fr, origin: str) -> None:
    s, db = ctx.settings, ctx.db
    com, com_parts = check_commercial(c, s, fr.offers, ctx.now, ctx.store, ctx.llm)
    need, need_parts = check_needs(c, s, ctx.now, ctx.store, ctx.llm)
    with db.transaction():
        db.execute("DELETE FROM receipts WHERE creator_key=? AND check_name IN ('commercial','needs')", (key,))
        for r in [com, *com_parts, need, *need_parts]:
            save_receipt(db, key, r, origin)
    if not com.passed:
        raise Reject(com.reason_code, stage="evidenced")
    if not need.passed:
        raise Reject(need.reason_code, stage="evidenced")
    depth = int(c.candidate.hint.get("depth", 0) or 0)
    links = list((c.profile.get("youtube") or {}).get("featured_channels", [])) + list(c.profile.get("site_links", []))
    links = [u if u.startswith("http") else f"https://www.youtube.com/channel/{u}" for u in links]
    n = snowball(ctx, key, links, depth)
    if n:
        ctx.log.log("snowball", creator_key=key, new_candidates=n)
