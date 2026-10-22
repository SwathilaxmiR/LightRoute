from __future__ import annotations

import csv

from receipts.models import Candidate, CollectedCreator
from receipts.pipeline.s5_identity import anchor, review_fuzzy, web_id
from receipts.util.urls import normalise_handle
from scripts.make_synthetic import SynthCreator, input_ref

SHARED = ["linktr.ee", "substack.com", "medium.com", "youtube.com"]


def test_youtube_and_podcast_sharing_site_merge(run_world):
    yt = SynthCreator(n=1, kind="youtube")
    pod = SynthCreator(n=1, kind="podcast")  # same n -> same website domain, different feed id
    summary, db = run_world([yt, pod], [input_ref(yt), input_ref(pod)], stop_after="s2")
    assert summary.dupes_removed == 1
    assert db.scalar("SELECT COUNT(*) FROM creators") == 1
    key = db.scalar("SELECT creator_key FROM creators")
    plats = {r["platform"] for r in db.fetch_all("SELECT platform FROM identities WHERE creator_key=?", (key,))}
    assert {"youtube", "podcastindex", "rss", "website"} <= plats
    # podcast episodes moved onto the earlier (YouTube) creator
    assert db.scalar("SELECT COUNT(DISTINCT platform) FROM content_items WHERE creator_key=?", (key,)) == 2
    row = db.fetch_one("SELECT status, reject_reason, creator_key FROM candidates WHERE platform='podcast'")
    assert row["reject_reason"] == "duplicate_merged" and row["creator_key"] == key


def test_handle_variants_normalise_to_one():
    assert {normalise_handle("youtube", v) for v in ["@Name", "name", "https://youtube.com/@name/",
                                                      "https://www.youtube.com/@NAME"]} == {"name"}


def test_handle_candidate_merges_with_channel_id_candidate(run_world):
    c = SynthCreator(n=2, kind="youtube")
    from receipts.sources.youtube import BASE

    summary, db = run_world([c], [input_ref(c)], stop_after="s2")
    # same channel referenced by handle: forHandle fixture returns the same channel
    import json
    import os

    from receipts.http import write_fixture_file
    from scripts.make_synthetic import yt_channel_item

    ch = yt_channel_item(c.channel_id, c.name, "desc", "US", 1000, "UU" + c.channel_id[2:], "@" + c.slug)
    write_fixture_file(os.environ["RECEIPTS_FIXTURES"], f"{BASE}/channels",
                       {"part": "snippet,statistics,brandingSettings,contentDetails", "forHandle": "@" + c.slug},
                       json.dumps({"items": [ch]}))
    summary, db = run_world([c], ["@" + c.slug.upper()], build=False, stop_after="s2")
    assert summary.dupes_removed == 1 and db.scalar("SELECT COUNT(*) FROM creators") == 1


def test_shared_host_and_blocklisted_domain_never_anchor():
    c = CollectedCreator(candidate=Candidate(platform="website", source_id="web:linktr.ee/jane",
                                             url="https://linktr.ee/jane", source="input_file"),
                         profile={"website_url": "https://linktr.ee/jane"})
    assert not anchor(c, SHARED, ["linktr.ee"]).startswith("web:linktr.ee")
    assert web_id("https://jane.substack.com/p/x", SHARED) == "jane.substack.com"
    assert web_id("https://medium.com/@jane/post", SHARED) == "medium.com/@jane"
    assert web_id("https://medium.com/@bob", SHARED) != web_id("https://medium.com/@jane", SHARED)


def test_linktree_input_rejected_not_merged(run_world, tmp_path):
    summary, db = run_world([], ["https://linktr.ee/alice", "https://linktr.ee/bob"], build=False)
    assert db.scalar("SELECT COUNT(*) FROM creators") == 0
    assert summary.dupes_removed == 0
    assert {r["reject_reason"] for r in db.fetch_all("SELECT reject_reason FROM candidates")} == {"blocklisted_domain"}


def test_fuzzy_near_duplicates_reviewed_not_merged(db, tmp_path):
    db.insert("creators", {"creator_key": "cr_a", "display_name": "The Synthetic Pricing Show"})
    db.insert("creators", {"creator_key": "cr_b", "display_name": "Synthetic Pricing Show"})
    n = review_fuzzy(db, "cr_b", "Synthetic Pricing Show", tmp_path, 92)
    assert n == 1
    rows = list(csv.DictReader((tmp_path / "logs" / "review_possible_dupes.csv").open()))
    assert rows[0]["possible_dupe_key"] == "cr_a"
    assert db.scalar("SELECT COUNT(*) FROM creators") == 2


def test_second_ingest_of_same_input_adds_no_creators(run_world):
    world = [SynthCreator(n=i, kind="youtube" if i % 2 else "podcast") for i in range(3, 7)]
    refs = [input_ref(c) for c in world]
    s1, db = run_world(world, refs)
    n = db.scalar("SELECT COUNT(*) FROM creators")
    s2, db = run_world(world, refs, build=False)
    assert db.scalar("SELECT COUNT(*) FROM creators") == n
    assert s2.new_candidates == 0 and s2.produced == 0 and s2.unchanged == len(refs)
