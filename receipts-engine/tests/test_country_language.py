from __future__ import annotations

from datetime import UTC, datetime, timedelta

from receipts.checks.country_language import cctld_signal, evaluate_country, evaluate_language, to_iso2
from receipts.models import ContentItem

NOW = datetime(2026, 10, 21, tzinfo=UTC)
YT = ("youtube snippet.country", "https://www.youtube.com/channel/UCx", 1)


def test_declared_us_without_corroboration_is_unverified(settings):
    r = evaluate_country("US", YT, "https://janedoe.com", [("https://janedoe.com", 2, "I make videos about money.")],
                         [], settings.countries)
    assert r.result == "fail" and r.reason_code == "country_unverified"


def test_declared_gb_with_co_uk_passes(settings):
    r = evaluate_country("GB", YT, "https://janedoe.co.uk", [], [], settings.countries)
    assert r.result == "pass"
    assert "cctld:'janedoe.co.uk'" in r.quote_or_metric and r.details["declared"] == "GB"


def test_declared_in_with_dot_in_is_not_approved(settings):
    r = evaluate_country("IN", YT, "https://janedoe.in", [], [], settings.countries)
    assert r.result == "fail" and r.reason_code == "country_not_approved"


def test_name_and_postal_corroboration(settings):
    cfg = settings.countries
    ok = evaluate_country("CA", YT, "https://x.com", [("https://x.com/about", 3, "Studio in Toronto, ON M5V 2T6")], [], cfg)
    assert ok.result == "pass" and ok.snap_id == 3
    au = evaluate_country("AU", YT, "https://x.com", [("https://x.com/c", 4, "PO Box 1, Surry Hills NSW 2010")], [], cfg)
    assert au.result == "pass"
    # a year followed later by a state word must not count as an AU postcode
    no = evaluate_country("AU", YT, "https://x.com", [("https://x.com/c", 4, "Founded 2019. We love WA beaches")], [], cfg)
    assert no.reason_code == "country_unverified"


def test_disagreeing_signal_does_not_corroborate(settings):
    r = evaluate_country("US", YT, "https://janedoe.co.uk", [], [], settings.countries)
    assert r.reason_code == "country_unverified"


def test_schema_org_declares_when_no_platform_country(settings):
    r = evaluate_country(None, None, "https://show.com", [("https://show.com/about", 5, "Recorded in Dublin")],
                         [("https://show.com", 6, "IE")], settings.countries)
    assert r.result == "pass" and "schema.org" in r.quote_or_metric


def test_no_declared_country_is_unverified(settings):
    r = evaluate_country(None, None, "https://show.co.uk", [("u", 1, "London based")], [], settings.countries)
    assert r.reason_code == "country_unverified"


def test_generic_cctld_is_not_a_signal(settings):
    assert cctld_signal("https://jane.io", settings.countries) is None
    assert cctld_signal("https://jane.fm", settings.countries) is None
    assert to_iso2("UK", settings.countries) == "GB"
    assert to_iso2("United Kingdom", settings.countries) == "GB"


EN = [
    "How I priced my coaching offer and doubled revenue in one year",
    "The morning routine that keeps my small business running smoothly",
    "Five lessons from interviewing a hundred startup founders",
    "Why most creators burn out and what to do about it instead",
]
ES = [
    "Cómo fijar el precio de tu primera oferta de consultoría paso a paso",
    "La rutina de la mañana que mantiene mi pequeño negocio funcionando",
]


def _items(titles):
    return [ContentItem(platform="youtube", item_id=f"v{i}", url=f"https://y.test/{i}", title=t,
                        published_at=NOW - timedelta(days=i + 1)) for i, t in enumerate(titles)]


def test_three_of_five_english_is_not_english(settings):
    r = evaluate_language(_items(EN[:3] + ES), settings.thresholds, settings.text)
    assert r.result == "fail" and r.reason_code == "not_english"
    assert r.quote_or_metric.startswith("english_items=3; checked=5")


def test_four_of_five_english_passes(settings):
    r = evaluate_language(_items(EN + ES[:1]), settings.thresholds, settings.text)
    assert r.result == "pass" and r.quote_or_metric.startswith("english_items=4; checked=5")


def test_only_latest_five_items_are_checked(settings):
    items = _items(EN + ES[:1] + ES)  # the 6th and 7th (oldest) are Spanish and ignored
    assert evaluate_language(items, settings.thresholds, settings.text).result == "pass"
