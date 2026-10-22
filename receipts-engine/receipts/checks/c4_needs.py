"""Check c4: needs. Pass only with >= need_min_signals independent signals including >= need_min_strong strong.

Signals: no_shortform (strong), captions_gap (strong, demotable), backlog (strong),
inconsistent_publishing (medium), team_workflow (medium), single_platform (medium).
Optional judge_need on creator text (medium, verified quote only)."""
from __future__ import annotations

import re
from datetime import timedelta
from urllib.parse import urlsplit

from receipts.checks.base import CheckResult, failed, metric, passed
from receipts.checks.c2_active import dedupe_items, is_longform
from receipts.checks.sources_text import creator_texts
from receipts.llm.client import accept_llm_claim
from receipts.models import CollectedCreator
from receipts.util.dates import max_gap_days, median_gap_days, within_days
from receipts.util.text import sentence_around, verify_quote
from receipts.util.urls import registrable_domain

SIGNALS = ["no_shortform", "captions_gap", "backlog", "inconsistent_publishing", "team_workflow", "single_platform"]
TRANSCRIPT_OR_CLIP = re.compile(r"\b(transcript|transcription|clips?|shorts?|highlights?)\b", re.I)


def strength(signal: str, s) -> str:
    if signal == "captions_gap":
        return s.evidence.captions_gap_strength
    return "strong" if signal in ("no_shortform", "backlog") else "medium"


def _yt(c: CollectedCreator):
    return c.profile.get("youtube")


def sig_no_shortform(c, s, now) -> CheckResult:
    yt = _yt(c)
    if not yt:
        return failed("needs", "not_video_first", "no_shortform")
    thr = s.thresholds
    vids = [i for i in c.items if i.platform == "youtube" and within_days(i.published_at, thr.window_days_long, now)]
    longs = [i for i in vids if is_longform(i.duration_s, thr)]
    maybe_short = [i for i in vids if i.is_short or (i.duration_s and i.duration_s <= s.evidence.shorts_conservative_max_seconds)]
    m = metric(longform_60d=len(longs), short_or_under_180s_60d=len(maybe_short), items_checked=len(vids),
               urls=[i.url for i in vids[:5]])
    ev = f"https://www.youtube.com/channel/{yt['channel_id']}/shorts"
    if longs and not maybe_short:
        return passed("needs", "no_shortform", evidence_url=ev, quote_or_metric=m, snap_id=longs[0].snap_id)
    return failed("needs", "has_shortform" if maybe_short else "no_longform", "no_shortform", evidence_url=ev,
                  quote_or_metric=m)


def sig_captions_gap(c, s, now) -> CheckResult:
    """YouTube: contentDetails.caption on recent long-form videos. Podcast-only creators: the
    podcast:transcript tag on recent long episodes (stored in has_captions by sources/rss.py)."""
    thr = s.thresholds
    field = "contentDetails.caption"
    longs = sorted([i for i in c.items if i.platform == "youtube" and is_longform(i.duration_s, thr)
                    and i.has_captions is not None], key=lambda i: i.published_at, reverse=True)[: thr.caption_sample_size]
    if not longs:
        field = "podcast:transcript tag"
        longs = sorted([i for i in c.items if i.platform == "podcast" and is_longform(i.duration_s, thr)
                        and i.has_captions is not None], key=lambda i: i.published_at, reverse=True)[: thr.caption_sample_size]
    if not longs:
        return failed("needs", "no_caption_data", "captions_gap")
    missing = [i for i in longs if i.has_captions is False]
    frac = len(missing) / len(longs)
    m = metric(missing=len(missing), sampled=len(longs), missing_ids=[i.item_id for i in missing], field=field)
    if frac >= thr.caption_missing_fraction and missing:
        return passed("needs", "captions_gap", evidence_url=missing[0].url, quote_or_metric=m, snap_id=missing[0].snap_id,
                      details={"missing": len(missing), "sampled": len(longs), "field": field})
    return failed("needs", "captions_present", "captions_gap", quote_or_metric=m)


def sig_backlog(c, s, now) -> CheckResult:
    thr = s.thresholds
    yt = _yt(c)
    fm = c.feed_meta or {}
    if yt and (yt.get("video_count") or 0) >= thr.backlog_min_items:
        vids60 = [i for i in c.items if i.platform == "youtube" and within_days(i.published_at, thr.window_days_long, now)]
        clips = [i for i in vids60 if i.is_short or (i.duration_s and i.duration_s <= s.evidence.shorts_conservative_max_seconds)]
        m = metric(public_videos=yt["video_count"], shorts_or_clips_60d=len(clips))
        if not clips:
            return passed("needs", "backlog", evidence_url=yt["url"] + "/videos", quote_or_metric=m, snap_id=yt.get("snap_id"),
                          details={"n": yt["video_count"], "items": "videos"})
        return failed("needs", "clips_exist", "backlog", quote_or_metric=m)
    pod = [i for i in c.items if i.platform == "podcast"]
    n = max(len(pod), fm.get("pi_episode_count") or 0, fm.get("episode_count") or 0)
    if c.feed_meta is not None and n >= thr.backlog_min_items:
        old = [i for i in pod if i.published_at < now - timedelta(days=s.evidence.backlog_old_item_days)]
        with_tx = [i for i in old if i.has_captions or TRANSCRIPT_OR_CLIP.search(i.description or "")]
        m = metric(episodes=n, older_than_90d=len(old), with_transcript_or_clip=len(with_tx))
        if old and not with_tx:
            return passed("needs", "backlog", evidence_url=fm.get("url"), quote_or_metric=m, snap_id=fm.get("snap_id"),
                          details={"n": n, "items": "episodes"})
        return failed("needs", "transcripts_exist" if with_tx else "no_old_items", "backlog", quote_or_metric=m)
    return failed("needs", "small_catalogue", "backlog", quote_or_metric=metric(items=max(n, (yt or {}).get("video_count") or 0)))


def sig_inconsistent(c, s, now) -> CheckResult:
    ev_s = s.evidence
    items = [i for i in dedupe_items(c.items) if within_days(i.published_at, ev_s.cadence_window_days, now)]
    dates = sorted(i.published_at for i in items)
    yt = _yt(c)
    ev = (yt["url"] + "/videos") if yt else (c.feed_meta or {}).get("url") or c.candidate.url
    half = ev_s.cadence_compare_days
    recent = sum(1 for d in dates if d >= now - timedelta(days=half))
    prior = sum(1 for d in dates if now - timedelta(days=2 * half) <= d < now - timedelta(days=half))
    med, mx = median_gap_days(dates), max_gap_days(dates)
    m = metric(items_90d=len(dates), median_gap_days=round(med, 1) if med is not None else "na",
               max_gap_days=round(mx, 1) if mx is not None else "na", last_45d=recent, prior_45d=prior)
    if len(dates) >= 3 and med and mx is not None and mx >= s.thresholds.cadence_gap_multiple * med:
        return passed("needs", "inconsistent_publishing", evidence_url=ev, quote_or_metric=m,
                      details={"rule": "gap", "max_gap": round(mx), "median_gap": round(med, 1)})
    if prior >= 2 and recent < prior / 2:
        return passed("needs", "inconsistent_publishing", evidence_url=ev, quote_or_metric=m,
                      details={"rule": "slowdown", "recent": recent, "prior": prior})
    return failed("needs", "steady_cadence", "inconsistent_publishing", quote_or_metric=m)


def sig_team_workflow(c, s, now, store) -> CheckResult:
    pat = re.compile(r"\b(" + s.evidence.team_workflow_pattern + r")", re.I)
    for src in creator_texts(c, include_items=True):
        h = pat.search(src.text)
        if h:
            q = sentence_around(src.text, h.start(), h.end())
            if src.snap_id and verify_quote(q, store.load_snapshot_text(src.snap_id), s.text.quote_min_chars):
                return passed("needs", "team_workflow", evidence_url=src.url, snap_id=src.snap_id, quote_or_metric=q)
    return failed("needs", "no_team_mention", "team_workflow")


def sig_single_platform(c, s, now) -> CheckResult:
    m_cfg = s.monetization
    platforms = [p for p in ("youtube", "podcast") if any(i.platform == p for i in c.items)]
    if len(platforms) != 1:
        return failed("needs", "multi_platform", "single_platform", quote_or_metric=metric(platforms=platforms))
    own = platforms[0]
    own_domain = "youtube.com" if own == "youtube" else None
    links = list(c.profile.get("site_links", [])) + list(c.profile.get("links", []))
    others = []
    for u in links:
        reg = registrable_domain(u)
        path = urlsplit(u).path.lower()
        if reg in m_cfg.get("other_platform_domains", []) and reg != own_domain:
            others.append(u)
        elif any(path.startswith(p) for p in m_cfg.get("other_platform_paths", [])) and own != "podcast":
            others.append(u)
    site = c.profile.get("website_url")
    home_sid = c.website_pages.get(site, (None, ""))[0] if site else None
    ev = site or ((_yt(c) or {}).get("url")) or c.candidate.url
    m = metric(active_platforms=platforms, links_checked=len(links), other_platform_links=len(others))
    if not others:
        return passed("needs", "single_platform", evidence_url=ev, snap_id=home_sid, quote_or_metric=m)
    return failed("needs", "other_platforms_linked", "single_platform", quote_or_metric=m)


def need_signals(c: CollectedCreator, s, now, store, llm=None) -> list[CheckResult]:
    out = [sig_no_shortform(c, s, now), sig_captions_gap(c, s, now), sig_backlog(c, s, now),
           sig_inconsistent(c, s, now), sig_team_workflow(c, s, now, store), sig_single_platform(c, s, now)]
    for r in out:
        r.details["strength"] = strength(r.sub_key, s)
    have = {r.sub_key for r in out if r.passed}
    if llm is not None and not _meets(out, s):
        for src in [t for t in creator_texts(c, include_items=False) if t.kind == "site"][:3]:
            claim = llm.judge("judge_need", {"text": src.text[:6000]})
            lab = claim.get("label")
            if lab in SIGNALS and lab not in have:
                ok = accept_llm_claim(claim, store, src.snap_id, s.text.quote_min_chars)
                kw = {"evidence_url": src.url, "snap_id": src.snap_id, "quote_or_metric": claim.get("quote"),
                      "verified_by": "llm_verified"}
                r = (passed("needs", f"{lab}_stated", **kw) if ok
                     else failed("needs", "unverified_llm_claim", f"{lab}_stated", **kw))
                r.details["strength"] = "medium"
                out.append(r)
                if ok:
                    have.add(lab)
                    break
    return out


def _meets(signals: list[CheckResult], s) -> bool:
    ok = [r for r in signals if r.passed]
    strong = [r for r in ok if r.details.get("strength") == "strong"]
    return len(ok) >= s.thresholds.need_min_signals and len(strong) >= s.thresholds.need_min_strong


def check_needs(c: CollectedCreator, s, now, store, llm=None) -> tuple[CheckResult, list[CheckResult]]:
    sigs = need_signals(c, s, now, store, llm)
    ok = [r for r in sigs if r.passed]
    strong = [r for r in ok if r.details.get("strength") == "strong"]
    m = metric(signals=[f"{r.sub_key}({r.details['strength']})" for r in ok], strong=len(strong), total=len(ok),
               required=f"{s.thresholds.need_min_signals} incl {s.thresholds.need_min_strong} strong")
    ordered = sorted(ok, key=lambda r: (r.details.get("strength") != "strong", SIGNALS.index(r.sub_key.replace("_stated", ""))))
    if _meets(sigs, s):
        top = ordered[0]
        return passed("needs", evidence_url=top.evidence_url, snap_id=top.snap_id, quote_or_metric=m,
                      details={"signals": [r.sub_key for r in ordered], "strong": len(strong)}), sigs
    reason = "no_need_signal" if not ok else "insufficient_need_signals"
    return failed("needs", reason, quote_or_metric=m), sigs
