"""Checks c1..c4 (c5..c7 cases are added with their milestones). One pass and one fail per check."""
from __future__ import annotations

import pytest

from receipts.cache import SnapshotStore
from receipts.checks.c1_real_relevant import check_exclusions, decide_segment
from receipts.checks.c2_active import check_active
from receipts.checks.c3_commercial import check_commercial
from receipts.checks.c4_needs import (
    _meets,
    check_needs,
    sig_backlog,
    sig_captions_gap,
    sig_inconsistent,
    sig_no_shortform,
    sig_single_platform,
    sig_team_workflow,
)
from receipts.checks.offers import find_offers
from receipts.models import CheckResult
from tests.helpers import NOW, FakeLLM, make_creator, vid


@pytest.fixture
def store(db, tmp_path):
    return SnapshotStore(db, tmp_path / "snaps")


def weekly(n=12, secs=1500, **kw):
    return [vid(i, 1 + i * 7, secs, **kw) for i in range(n)]


# ------------------------------------------------------------------ c1 real_relevant
def test_c1_youtuber_passes(settings, store):
    c = make_creator(store, items=weekly())
    assert check_exclusions(c, settings) is None
    seg, r = decide_segment(c, settings, find_offers(c, settings, store), NOW)
    assert seg.value == "youtuber" and r.passed


def test_c1_agency_name_fails(settings, store):
    c = make_creator(store, name="Synthetic Marketing Agency", items=weekly())
    r = check_exclusions(c, settings)
    assert r.result == "fail" and r.reason_code == "excluded_keyword"


def test_c1_brand_scale_followers_fail(settings, store):
    c = make_creator(store, subs=9_000_000, items=weekly())
    assert check_exclusions(c, settings).reason_code == "agency_or_brand"


def test_c1_hallucinated_llm_quote_rejected(settings, store):
    c = make_creator(store, description="Weekly news and interviews for founders.", items=weekly())
    fake = FakeLLM({"judge_segment": {"label": "youtuber", "quote": "I am an independent video creator", "reason": "x"}})
    r = check_exclusions(c, settings, fake, store)
    assert r is not None and r.reason_code == "excluded_keyword"  # quote not in snapshot -> discarded


def test_c1_verified_llm_quote_accepted_for_ambiguous_keyword(settings, store):
    c = make_creator(store, description="Weekly news and interviews for founders.", items=weekly())
    fake = FakeLLM({"judge_segment": {"label": "youtuber", "quote": "Weekly news and interviews for founders", "reason": "x"}})
    assert check_exclusions(c, settings, fake, store) is None


def test_c1_expert_led_from_coaching_offer(settings, store):
    pages = {"https://synthcreator.test": "I help founders. Book a free call to start 1:1 coaching with me."}
    c = make_creator(store, items=weekly(6, secs=300), pages=pages)
    seg, r = decide_segment(c, settings, find_offers(c, settings, store), NOW)
    assert seg.value == "expert_led" and "Book a free call" in r.quote_or_metric


def test_c1_no_segment_fails_without_llm(settings, store):
    c = make_creator(store, items=[vid(1, 5, 300)])
    seg, r = decide_segment(c, settings, [], NOW)
    assert seg is None and r.reason_code == "not_creator_segment"


# ------------------------------------------------------------------ c2 active
def test_c2_pass_and_fail(settings, store):
    assert check_active(weekly(4), NOW, settings.thresholds).passed
    assert check_active([vid(1, 50, 300)], NOW, settings.thresholds).reason_code == "inactive"


# ------------------------------------------------------------------ c3 commercial: each class
def _com(settings, store, c):
    return check_commercial(c, settings, find_offers(c, settings, store), NOW, store)


def test_c3_sponsorship_from_description(settings, store):
    c = make_creator(store, items=[vid(1, 3, desc="Thanks for watching. This episode is sponsored by Example Widgets.")])
    agg, parts = _com(settings, store, c)
    assert agg.passed and parts[0].sub_key == "sponsorship" and "sponsored by Example Widgets" in parts[0].quote_or_metric


def test_c3_sponsorship_from_paid_placement_flag(settings, store):
    c = make_creator(store, items=[vid(1, 3, paid=True)])
    agg, parts = _com(settings, store, c)
    assert agg.passed and "paidProductPlacement=true" in parts[0].quote_or_metric


def test_c3_product_or_course_from_link(settings, store):
    pages = {"https://synthcreator.test": '<a href="https://synth.kajabi.com/offers/x">My flagship program</a>'}
    agg, parts = _com(settings, store, make_creator(store, items=weekly(2), pages=pages))
    assert agg.passed and {p.sub_key for p in parts} == {"product_or_course"}


def test_c3_membership(settings, store):
    pages = {"https://synthcreator.test": '<a href="https://www.patreon.com/synth">Support the show</a>'}
    agg, parts = _com(settings, store, make_creator(store, items=weekly(2), pages=pages))
    assert agg.passed and "membership" in {p.sub_key for p in parts}


def test_c3_affiliate(settings, store):
    c = make_creator(store, items=[vid(1, 3, desc="Gear list below. Use code SYNTH for a discount on the mic.")])
    agg, parts = _com(settings, store, c)
    assert agg.passed and parts[0].sub_key == "affiliate"


def test_c3_service(settings, store):
    pages = {"https://synthcreator.test/work-with-me": '<a href="https://calendly.com/synth/intro">Schedule</a>'}
    agg, parts = _com(settings, store, make_creator(store, items=weekly(2), pages=pages))
    assert agg.passed and "service" in {p.sub_key for p in parts}


def test_c3_none_fails(settings, store):
    agg, parts = _com(settings, store, make_creator(store, items=weekly(2)))
    assert agg.result == "fail" and agg.reason_code == "no_commercial_signal" and parts == []


def test_c3_hallucinated_llm_claim_not_counted(settings, store):
    c = make_creator(store, items=weekly(2), pages={"https://synthcreator.test": "Hello and welcome to my page."})
    fake = FakeLLM({"judge_commercial": {"label": "course", "quote": "Enrol in my paid course today", "reason": "x"}})
    agg, parts = check_commercial(c, settings, [], NOW, store, fake)
    assert agg.result == "fail"
    assert parts and parts[0].reason_code == "unverified_llm_claim" and not parts[0].passed


# ------------------------------------------------------------------ c4 needs: each signal pass + fail
def test_no_shortform(settings, store):
    assert sig_no_shortform(make_creator(store, items=weekly()), settings, NOW).passed
    with_short = weekly() + [vid(99, 2, 45)]
    assert sig_no_shortform(make_creator(store, items=with_short), settings, NOW).reason_code == "has_shortform"
    # 2-minute Shorts are possible: a 150 s item also blocks the signal
    assert not sig_no_shortform(make_creator(store, items=weekly() + [vid(98, 2, 150)]), settings, NOW).passed


def test_captions_gap(settings, store):
    assert sig_captions_gap(make_creator(store, items=weekly(captions=False)), settings, NOW).passed
    assert not sig_captions_gap(make_creator(store, items=weekly(captions=True)), settings, NOW).passed


def test_captions_gap_podcast_transcripts(settings, store):
    eps = [vid(i, 1 + 7 * i, 2500, platform="podcast", captions=False) for i in range(6)]
    r = sig_captions_gap(make_creator(store, youtube=False, feed=True, items=eps), settings, NOW)
    assert r.passed and "podcast:transcript" in r.quote_or_metric


def test_backlog(settings, store):
    assert sig_backlog(make_creator(store, video_count=150, items=weekly()), settings, NOW).passed
    assert sig_backlog(make_creator(store, video_count=40, items=weekly()), settings, NOW).reason_code == "small_catalogue"


def test_inconsistent_publishing(settings, store):
    gappy = [vid(i, d) for i, d in enumerate([1, 3, 5, 40, 42])]
    assert sig_inconsistent(make_creator(store, items=gappy), settings, NOW).passed
    assert not sig_inconsistent(make_creator(store, items=weekly()), settings, NOW).passed


def test_team_workflow(settings, store):
    c = make_creator(store, items=[vid(1, 2, desc="Produced by Synthetic Studio. Thanks for listening!")])
    r = sig_team_workflow(c, settings, NOW, store)
    assert r.passed and "Produced by Synthetic Studio" in r.quote_or_metric
    assert not sig_team_workflow(make_creator(store, items=weekly()), settings, NOW, store).passed


def test_single_platform(settings, store):
    assert sig_single_platform(make_creator(store, items=weekly()), settings, NOW).passed
    pages = {"https://synthcreator.test": '<a href="https://open.spotify.com/show/x">Listen on Spotify</a>'}
    r = sig_single_platform(make_creator(store, items=weekly(), pages=pages), settings, NOW)
    assert r.reason_code == "other_platforms_linked"


def _r(sub, strength, ok=True):
    r = CheckResult(check_name="needs", sub_key=sub, result="pass" if ok else "fail")
    r.details["strength"] = strength
    return r


def test_need_rule_only_one_medium_fails(settings):
    assert not _meets([_r("single_platform", "medium")], settings)
    assert not _meets([_r("single_platform", "medium"), _r("team_workflow", "medium")], settings)


def test_need_rule_one_strong_one_medium_passes(settings):
    assert _meets([_r("backlog", "strong"), _r("single_platform", "medium")], settings)
    assert not _meets([_r("backlog", "strong")], settings)


def test_check_needs_end_to_end(settings, store):
    # podcast-only, transcripts present, steady, small catalogue: only single_platform -> fail
    eps = [vid(i, 1 + 7 * i, 2500, platform="podcast", captions=True) for i in range(10)]
    agg, _ = check_needs(make_creator(store, youtube=False, feed=True, items=eps), settings, NOW, store)
    assert agg.result == "fail" and agg.reason_code == "insufficient_need_signals"
    # same show without transcripts: captions_gap (strong) + single_platform (medium) -> pass
    eps = [vid(i, 1 + 7 * i, 2500, platform="podcast", captions=False) for i in range(10)]
    agg, _ = check_needs(make_creator(store, youtube=False, feed=True, items=eps), settings, NOW, store)
    assert agg.passed and agg.details["signals"][0] == "captions_gap"


def test_need_llm_hallucination_rejected(settings, store):
    eps = [vid(i, 1 + 7 * i, 2500, platform="podcast", captions=True) for i in range(10)]
    c = make_creator(store, youtube=False, feed=True, items=eps,
                     pages={"https://synthcreator.test": "We publish every Tuesday."})
    fake = FakeLLM({"judge_need": {"label": "team_workflow", "quote": "I desperately need an editor", "reason": "x"}})
    agg, parts = check_needs(c, settings, NOW, store, fake)
    assert agg.result == "fail"
    stated = [p for p in parts if p.sub_key == "team_workflow_stated"]
    assert stated and stated[0].reason_code == "unverified_llm_claim"
