"""Source clients against replay fixtures (FIXTURE_MODE=1 semantics, no network).

Part 1 uses hand-built synthetic fixtures and always runs.
Part 2 checks the REAL fixtures recorded by `python scripts/record_fixtures.py`
(3 channels, 3 feeds, 3 sites); it needs tests/fixtures/recorded_manifest.json.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from receipts.cache import SnapshotStore
from receipts.http import HttpClient, QuotaExceeded, write_fixture_file
from receipts.sources import itunes
from receipts.sources.podcastindex import PodcastIndexAPI
from receipts.sources.rss import parse_feed
from receipts.sources.website import fetch_site, has_form, parse_schema_org
from receipts.sources.youtube import BASE as YT
from receipts.sources.youtube import YouTubeAPI
from scripts.make_synthetic import (
    SynthCreator,
    build_world,
    register_page,
    register_robots,
    register_youtube_search,
)
from tests.conftest import FIXTURES

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


@pytest.fixture
def world(tmp_path):
    fx = tmp_path / "fx"
    creators = [SynthCreator(n=1, kind="youtube", captions=False, shorts=True),
                SynthCreator(n=2, kind="podcast", captions=True, country="GB", place="London, England")]
    build_world(fx, creators, NOW)
    return fx, creators


@pytest.fixture
def store(db, tmp_path):
    return SnapshotStore(db, tmp_path / "snaps")


def client(settings, store, fx):
    return HttpClient(settings, store, fixture_mode=True, fixtures_dir=fx)


# ------------------------------------------------------------------ Part 1: synthetic
async def test_youtube_channel_uploads_videos(settings, store, db, world):
    fx, (c, _) = world
    async with client(settings, store, fx) as http:
        yt = YouTubeAPI(http, db, settings, api_key="")
        [ch] = await yt.channels([c.channel_id])
        assert ch.title == c.name and ch.country == "US" and ch.subscribers == 42000
        assert ch.uploads_playlist == "UU" + c.channel_id[2:]
        ups = await yt.uploads(ch.uploads_playlist)
        assert len(ups) == 30
        vids = await yt.videos([u.video_id for u in ups])
    assert len(vids) == 30
    longs = [v for v in vids if v.duration_s == 1500]
    shorts = [v for v in vids if v.is_short]
    assert longs and shorts and all(v.duration_s == 45 for v in shorts)
    assert all(v.has_captions is False for v in vids)
    assert vids[0].paid_placement is False
    assert yt.ledger.run_units == 3  # channels 1 + playlistItems 1 + videos 1
    assert store.get(vids[0].snap_id)["data_origin"] == "synthetic"


async def test_youtube_hidden_subscribers(settings, store, db, tmp_path):
    fx = tmp_path / "fx"
    build_world(fx, [SynthCreator(n=3, kind="youtube", subs=None)], NOW)
    async with client(settings, store, fx) as http:
        [ch] = await YouTubeAPI(http, db, settings, api_key="").channels([SynthCreator(n=3, kind="youtube").channel_id])
    assert ch.subscribers is None and ch.hidden_subscribers


async def test_youtube_search_spends_100_units(settings, store, db, tmp_path):
    fx = tmp_path / "fx"
    register_youtube_search(fx, "business podcast", ["UCsynth00000000000000001", "UCsynth00000000000000002"])
    async with client(settings, store, fx) as http:
        yt = YouTubeAPI(http, db, settings, api_key="")
        ids, tok = await yt.search_channels("business podcast")
    assert ids == ["UCsynth00000000000000001", "UCsynth00000000000000002"] and tok is None
    assert yt.ledger.run_units == 100 and yt.ledger.spent == 100


async def test_quota_ledger_stops_before_limit(settings, store, db, tmp_path):
    s = settings.model_copy(deep=True)
    s.quota.youtube_daily_units = 150
    fx = tmp_path / "fx"
    register_youtube_search(fx, "q", [])
    # live mode (the ledger is not enforced on replays); the transport answers like the API would
    import httpx

    transport = httpx.MockTransport(lambda req: httpx.Response(200, json={"items": []}))
    async with HttpClient(s, store, fixture_mode=False, transport=transport) as http:
        yt = YouTubeAPI(http, db, s, api_key="dummy-test-key")
        await yt.search_channels("q")  # 100 of 135 allowed
        await yt.search_channels("q")  # identical request: served from the snapshot cache, costs nothing
        assert yt.ledger.spent == 100
        with pytest.raises(QuotaExceeded):
            await yt.search_channels("another query")


async def test_youtube_quota_403_marks_ledger_exhausted(settings, store, db, tmp_path):
    fx = tmp_path / "fx"
    body = json.dumps({"error": {"code": 403, "errors": [{"reason": "quotaExceeded"}]}})
    write_fixture_file(fx, f"{YT}/channels", {"part": "snippet,statistics,brandingSettings,contentDetails",
                                              "id": "UCx", "maxResults": 50}, body, status=403)
    async with client(settings, store, fx) as http:
        yt = YouTubeAPI(http, db, settings, api_key="")
        with pytest.raises(QuotaExceeded):
            await yt.channels(["UCx"])
    assert yt.ledger.remaining == 0


async def test_podcastindex_and_rss(settings, store, world):
    fx, (_, p) = world
    async with client(settings, store, fx) as http:
        pi = PodcastIndexAPI(http, key="", secret="")
        feed = await pi.by_feed_id(p.feed_id)
        eps = await pi.episodes_by_feed_id(p.feed_id)
        parsed = await parse_feed(http, feed.url)
    assert feed.title == p.name and feed.link == p.site and feed.episode_count == 40 and feed.language == "en"
    assert len(eps) == 40 and eps[0].duration_s == 2530
    meta, items = parsed
    assert meta.owner_email == f"hello@{p.slug}.test" and meta.link == p.site
    assert meta.transcript_items == 40 and len(items) == 40
    assert items[0].duration_s == 42 * 60 + 10
    assert "Brought to you by Example Widgets" in items[0].description


def test_itunes_duration_formats():
    from receipts.util.dates import parse_hms
    assert parse_hms("01:02:03") == 3723
    assert parse_hms("42:10") == 2530
    assert parse_hms("2530") == 2530
    assert parse_hms("") is None


async def test_website_fetch_site_picks_relevant_pages(settings, store, world):
    fx, (c, _) = world
    async with client(settings, store, fx) as http:
        pages = await fetch_site(c.site, http)
    assert set(pages) == {c.site, c.site + "/about", c.site + "/contact", c.site + "/courses"}
    sid, text = pages[c.site + "/contact"]
    assert f"hello@{c.slug}.test" in text
    raw = store.load_raw(sid).decode()
    assert has_form(raw)
    ld = parse_schema_org(store.load_raw(pages[c.site][0]).decode())
    assert ld["addressCountry"] == ["US"] and "Person" in ld["types"]


async def test_website_respects_robots_in_replay(settings, store, tmp_path):
    fx = tmp_path / "fx"
    site = "https://robotsblock.test"
    register_robots(fx, site, "User-agent: *\nDisallow: /contact\n")
    register_page(fx, site, '<html><body><a href="/contact">c</a><a href="/about">a</a></body></html>')
    register_page(fx, site + "/contact", "<html><body>hidden</body></html>")
    register_page(fx, site + "/about", "<html><body>about us</body></html>")
    s = settings.model_copy(deep=True)
    async with HttpClient(s, store, fixture_mode=True, fixtures_dir=fx) as http:
        pages = await fetch_site(site, http)
    assert set(pages) == {site, site + "/about"}


async def test_itunes_lookup(settings, store, tmp_path):
    fx = tmp_path / "fx"
    write_fixture_file(fx, "https://itunes.apple.com/lookup", {"id": 1, "entity": "podcast"},
                       json.dumps({"resultCount": 1, "results": [{"collectionName": "Synthetic Show",
                                                                   "feedUrl": "https://s.test/feed", "trackCount": 9}]}))
    async with client(settings, store, fx) as http:
        r = await itunes.lookup(http, 1)
    assert r["name"] == "Synthetic Show" and r["track_count"] == 9


# ------------------------------------------------------------------ Part 2: recorded real fixtures
MANIFEST = FIXTURES / "recorded_manifest.json"
needs_recording = pytest.mark.skipif(
    not MANIFEST.exists(),
    reason="real fixtures not recorded yet: put keys in .env, fill tests/fixtures/record_targets.yaml, "
           "then run `python scripts/record_fixtures.py` (BUILD_SPEC M2, section 16 item 6)",
)


@pytest.fixture
def manifest():
    return json.loads(MANIFEST.read_text())


@needs_recording
def test_manifest_has_3_of_each(manifest):
    assert len(manifest["youtube"]) >= 3 and len(manifest["podcast"]) >= 3 and len(manifest["website"]) >= 3


@needs_recording
async def test_recorded_youtube_channels(settings, store, db, manifest):
    async with HttpClient(settings, store, fixture_mode=True, fixtures_dir=FIXTURES) as http:
        yt = YouTubeAPI(http, db, settings, api_key="")
        for cid in manifest["youtube"]:
            [ch] = await yt.channels([cid])
            assert ch.title and ch.uploads_playlist
            ups = await yt.uploads(ch.uploads_playlist)
            vids = await yt.videos([u.video_id for u in ups])
            assert vids and all(v.published_at <= datetime.now(UTC) + timedelta(days=1) for v in vids)
            assert store.get(vids[0].snap_id)["data_origin"] == "fixture"


@needs_recording
async def test_recorded_podcast_feeds(settings, store, manifest):
    async with HttpClient(settings, store, fixture_mode=True, fixtures_dir=FIXTURES) as http:
        pi = PodcastIndexAPI(http, key="", secret="")
        for fid in manifest["podcast"]:
            feed = await pi.by_feed_id(fid)
            assert feed and feed.url
            parsed = await parse_feed(http, feed.url)
            assert parsed and parsed[1], f"feed {feed.url} has no items"


@needs_recording
async def test_recorded_websites(settings, store, manifest):
    async with HttpClient(settings, store, fixture_mode=True, fixtures_dir=FIXTURES) as http:
        for site in manifest["website"]:
            pages = await fetch_site(site, http)
            assert site in pages and pages[site][1].strip()
