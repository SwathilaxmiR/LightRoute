from __future__ import annotations

from receipts.pipeline.s0_ingest import classify, insert_candidates, load_input

SHARED = ["substack.com", "youtube.com", "linktr.ee"]


def test_classify_all_input_forms():
    cid = "UC" + "a" * 22
    assert classify(cid, SHARED).source_id == cid
    assert classify(f"https://www.youtube.com/channel/{cid}", SHARED).source_id == cid
    h = classify("https://youtube.com/@JaneDoe/", SHARED)
    assert h.platform == "youtube" and h.source_id == "@janedoe"
    assert classify("@JaneDoe", SHARED).source_id == "@janedoe"
    f = classify("https://feeds.example.com/show.xml", SHARED)
    assert f.platform == "podcast" and f.source_id.startswith("rss:")
    assert classify("pi:123456", SHARED).source_id == "123456"
    w = classify("https://www.janedoe.co.uk/about", SHARED)
    assert w.platform == "website" and w.source_id == "web:janedoe.co.uk"
    s = classify("https://jane.substack.com", SHARED)
    assert s.source_id == "web:jane.substack.com"  # shared host keeps the subdomain
    assert classify("https://www.youtube.com/watch?v=abc", SHARED) is None
    assert classify("just some words", SHARED) is None


def test_csv_with_header_dedupes_and_rejects(tmp_path):
    p = tmp_path / "in.csv"
    cid = "UC" + "b" * 22
    p.write_text(f"name,channel_id\nJane,{cid}\nJane again,{cid}\nBad,not a thing\n")
    r = load_input(p, None, SHARED)
    assert [c.source_id for c in r.candidates] == [cid]
    assert r.rejects == ["Bad,not a thing"]


def test_csv_without_header_uses_first_column(tmp_path):
    p = tmp_path / "in.csv"
    p.write_text("https://janedoe.com,extra\n@someone,x\n")
    r = load_input(p, None, SHARED)
    assert {c.source_id for c in r.candidates} == {"web:janedoe.com", "@someone"}


def test_txt_and_yaml_and_keywords(tmp_path):
    t = tmp_path / "in.txt"
    t.write_text("# comment\nhttps://janedoe.com\n\n")
    assert len(load_input(t, None, SHARED).candidates) == 1
    y = load_input("config/seeds.yaml", None, SHARED)
    assert y.is_seeds and not y.candidates
    k = load_input("marketing podcast, sales coach", None, SHARED)
    assert k.keywords == ["marketing podcast", "sales coach"]


def test_second_ingest_inserts_nothing(db, tmp_path):
    p = tmp_path / "in.txt"
    p.write_text("https://janedoe.com\n@someone\n")
    cands = load_input(p, None, SHARED).candidates
    assert insert_candidates(db, cands, 1) == 2
    assert insert_candidates(db, cands, 2) == 0
