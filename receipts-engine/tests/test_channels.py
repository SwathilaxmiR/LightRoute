from __future__ import annotations

import pytest

from receipts.cache import SnapshotStore
from receipts.checks.c5_reachable import extract_routes, reachable_result, score_routes, select, verify_routes
from receipts.manual import ManualCheckError, record_manual_check
from receipts.pipeline.s6_channels import save_routes
from tests.helpers import NOW, make_creator, vid

SITE = "https://synthcreator.test"


@pytest.fixture
def store(db, tmp_path):
    return SnapshotStore(db, tmp_path / "snaps")


def routes_for(settings, store, c, mx=None, manual=None):
    rs = extract_routes(c, store, settings)
    verify_routes(rs, c, settings, NOW, mx or {}, manual or {})
    score_routes(rs, c, settings, 0)
    return rs


def creator(store, pages, **kw):
    return make_creator(store, name="Synthetic Creator", items=[vid(1, 3)], pages=pages, **kw)


def test_email_on_creator_page_passes(settings, store):
    c = creator(store, {SITE + "/contact": "Synthetic Creator. For business inquiries: hello@synthcreator.test"})
    rs = routes_for(settings, store, c, mx={"synthcreator.test": True})
    email = next(r for r in rs if r.channel_type == "email")
    assert email.handle_or_url == "hello@synthcreator.test" and email.verified and email.business_language
    assert email.source_url == SITE + "/contact" and "hello@synthcreator.test" in email.source_quote


def test_email_only_on_third_party_page_rejected(settings, store):
    c = creator(store, {SITE: "Welcome to Synthetic Creator.",
                        "https://podcastdirectory.test/synth": "Contact the host: host@synthcreator.test"})
    rs = routes_for(settings, store, c, mx={"synthcreator.test": True})
    assert not [r for r in rs if r.channel_type == "email"]


def test_guessed_info_address_never_produced(settings, store):
    c = creator(store, {SITE + "/contact": "Synthetic Creator. Get in touch using the form below."})
    rs = routes_for(settings, store, c, mx={"synthcreator.test": True})
    assert all(not r.handle_or_url.startswith(("info@", "hello@", "contact@")) for r in rs)
    assert not [r for r in rs if r.channel_type == "email"]


def test_email_without_mx_fails(settings, store):
    c = creator(store, {SITE + "/contact": "Synthetic Creator business inquiries: hello@nomx-synth.test"})
    rs = routes_for(settings, store, c, mx={"nomx-synth.test": False})
    email = next(r for r in rs if r.channel_type == "email")
    assert email.mx_ok is False and not email.verified


def test_linkedin_without_manual_check_is_unverified_and_never_best(settings, store):
    c = creator(store, {SITE: 'Synthetic Creator <a href="https://www.linkedin.com/in/synthetic-creator">LinkedIn</a>'})
    rs = routes_for(settings, store, c)
    li = next(r for r in rs if r.channel_type == "linkedin")
    assert li.name_match and not li.verified and li.assisted_send
    best, backup = select(rs)
    assert best is None or best.channel_type != "linkedin"


def test_linkedin_with_manual_check_verifies(settings, store):
    c = creator(store, {SITE: 'Synthetic Creator <a href="https://www.linkedin.com/in/synthetic-creator">LinkedIn</a>'})
    manual = {("linkedin", "https://www.linkedin.com/in/synthetic-creator"):
              {"manual_check_path": "out/manual_checks/x.png", "last_active": "2026-10-10"}}
    li = next(r for r in routes_for(settings, store, c, manual=manual) if r.channel_type == "linkedin")
    assert li.verified and li.active_proof == "manual"


def test_business_language_absent_not_best(settings, store):
    c = creator(store, {SITE + "/about": "Synthetic Creator. Say hi at hi@synthcreator.test anytime",
                        SITE + "/contact": "Synthetic Creator. Sponsorship and business inquiries via the form."
                                           "<form><input></form>"})
    rs = routes_for(settings, store, c, mx={"synthcreator.test": True})
    email = next(r for r in rs if r.channel_type == "email")
    assert email.verified and not email.business_language
    best, backup = select(rs)
    assert best.channel_type == "form" and backup.channel_type == "email"
    assert reachable_result(best, backup, rs).passed


def test_contact_page_without_form_is_website_backup(settings, store):
    c = creator(store, {SITE + "/contact": "Synthetic Creator business inquiries: hello@synthcreator.test"})
    best, backup = select(routes_for(settings, store, c, mx={"synthcreator.test": True}))
    assert best.channel_type == "email" and backup.channel_type == "website"


def test_no_backup_fails_reachable(settings, store):
    c = creator(store, {SITE + "/about": "Synthetic Creator business inquiries: hello@synthcreator.test"})
    rs = routes_for(settings, store, c, mx={"synthcreator.test": True})
    best, backup = select(rs)
    assert best and backup is None
    assert reachable_result(best, backup, rs).reason_code == "channel_unverified"


def test_feed_owner_email_needs_corroboration(settings, store):
    c = creator(store, {SITE: "Synthetic Creator homepage"}, feed=True, feed_desc="A show.")
    c.feed_meta["owner_email"] = "owner@synthcreator.test"
    assert not [r for r in extract_routes(c, store, settings) if r.channel_type == "email"]
    c2 = creator(store, {SITE + "/contact": "Synthetic Creator. Business: owner@synthcreator.test"}, feed=True)
    c2.feed_meta["owner_email"] = "owner@synthcreator.test"
    assert [r for r in extract_routes(c2, store, settings) if r.channel_type == "email"]


def test_manual_check_command_flow(settings, store, db, tmp_path):
    c = creator(store, {SITE: 'Synthetic Creator <a href="https://www.linkedin.com/in/synthetic-creator">in</a>'})
    db.insert("creators", {"creator_key": "cr_synth", "display_name": "Synthetic Creator"})
    rs = routes_for(settings, store, c)
    save_routes(db, "cr_synth", rs)
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"\x89PNG")
    s = settings.model_copy(deep=True)
    res = record_manual_check(db, s, "cr_synth", "linkedin", "2026-10-15", shot, "posted weekly", NOW)
    assert res["verified"]
    row = db.fetch_one("SELECT * FROM channels WHERE channel_type='linkedin'")
    assert row["active_proof"] == "manual" and row["verified"] == 1 and row["manual_check_path"]
    with pytest.raises(ManualCheckError):
        record_manual_check(db, s, "cr_synth", "email", "2026-10-15", shot, "", NOW)
    old = record_manual_check(db, s, "cr_synth", "linkedin", "2026-01-01", shot, "", NOW)
    assert not old["verified"]
