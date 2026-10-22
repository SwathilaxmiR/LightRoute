from __future__ import annotations

from types import SimpleNamespace

from receipts.llm.client import LLM, accept_llm_claim, make_llm


class FakeAnthropic:
    def __init__(self, texts):
        self.texts = list(texts)
        self.requests = []
        self.messages = self

    def create(self, **kw):
        self.requests.append(kw)
        t = self.texts.pop(0)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=t)], stop_reason="end_turn",
                               usage=SimpleNamespace(input_tokens=100, output_tokens=20))


def test_judge_caches_and_counts_tokens(settings, db):
    fake = FakeAnthropic(['{"label": "youtuber", "quote": "weekly videos about money", "reason": "r"}'])
    llm = LLM(settings, db, client=fake)
    a = llm.judge("judge_segment", {"text": "x"})
    b = llm.judge("judge_segment", {"text": "x"})
    assert a == b and a["label"] == "youtuber"
    assert len(fake.requests) == 1 and llm.in_tokens == 100 and llm.out_tokens == 20
    req = fake.requests[0]
    assert req["model"] == settings.llm.model and "temperature" not in req
    assert req["output_config"]["format"]["type"] == "json_schema"


def test_invalid_json_one_repair_then_unknown(settings, db):
    fake = FakeAnthropic(["not json", "still not json"])
    r = LLM(settings, db, client=fake).judge("judge_segment", {"text": "y"})
    assert r == {"label": "unknown", "quote": "", "reason": "invalid_json"} and len(fake.requests) == 2


def test_repair_retry_succeeds(settings, db):
    fake = FakeAnthropic(["oops", '{"label": "none", "quote": "", "reason": "nothing"}'])
    assert LLM(settings, db, client=fake).judge("judge_commercial", {"text": "z"})["label"] == "none"


def test_no_key_or_fixture_mode_means_rules_only(settings, db, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert make_llm(settings, db) is None
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x" * 10)
    assert make_llm(settings, db, fixture_mode=True) is None


def test_accept_llm_claim_requires_snapshot_text():
    assert accept_llm_claim({"quote": "join my membership today"}, None, None, text="Please join my membership today!")
    assert not accept_llm_claim({"quote": "join my membership today"}, None, None, text="Buy my book")
    assert not accept_llm_claim({"quote": "join my membership today"}, None, None)
