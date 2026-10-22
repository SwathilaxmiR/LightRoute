"""LLM judge (BUILD_SPEC 8.6). LLM output is untrusted: quotes are accepted only via accept_llm_claim.

Model is `llm.model` (claude-haiku-5-5 by default). Haiku 5.5 rejects non-default sampling
parameters, so `llm.temperature` is not sent; determinism comes from the cache and from code
deciding outcomes. Structured output via output_config.format (json_schema).
"""
from __future__ import annotations

import hashlib
import json
import os
from importlib import resources
from typing import Any

from receipts.db import DB
from receipts.util.dates import utcnow
from receipts.util.text import verify_quote

LABELS = {
    "judge_segment": ["podcaster", "youtuber", "expert_led", "agency_or_brand", "publication_or_directory",
                      "fan_account", "unknown"],
    "judge_commercial": ["sponsorship", "product", "course", "membership", "subscription", "affiliate", "service",
                         "none", "unknown"],
    "judge_need": ["captions_gap", "repurposing_gap", "backlog", "inconsistent_publishing", "team_workflow",
                   "single_platform", "none", "unknown"],
}
UNKNOWN = {"label": "unknown", "quote": "", "reason": "invalid_json"}


def claim_schema(prompt_name: str) -> dict:
    return {
        "type": "object",
        "properties": {
            "label": {"type": "string", "enum": LABELS[prompt_name]},
            "quote": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["label", "quote", "reason"],
        "additionalProperties": False,
    }


HOOK_SCHEMA = {"type": "object", "properties": {"hook": {"type": "string"}}, "required": ["hook"],
               "additionalProperties": False}


def load_prompt(name: str) -> str:
    return resources.files("receipts.llm.prompts").joinpath(f"{name}.md").read_text(encoding="utf-8")


class LLM:
    def __init__(self, settings, db: DB, client: Any = None):
        self.s = settings.llm
        self.db = db
        self._client = client
        self.in_tokens = 0
        self.out_tokens = 0
        self.calls = 0

    @property
    def client(self):
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic()
        return self._client

    @property
    def cost_usd(self) -> float:
        return (self.in_tokens * self.s.usd_per_mtok_in + self.out_tokens * self.s.usd_per_mtok_out) / 1e6

    def _key(self, prompt_name: str, payload: dict) -> str:
        raw = prompt_name + self.s.model + json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode()).hexdigest()

    def judge(self, prompt_name: str, payload: dict, schema: dict | None = None) -> dict:
        schema = schema or claim_schema(prompt_name)
        key = self._key(prompt_name, payload)
        if self.s.cache:
            row = self.db.fetch_one("SELECT response FROM llm_cache WHERE key=?", (key,))
            if row:
                return json.loads(row["response"])
        prompt = load_prompt(prompt_name).replace("{payload}", json.dumps(payload, ensure_ascii=False, indent=1))
        result = None
        for attempt in range(2):  # one repair retry on invalid JSON
            text = self._call(prompt if attempt == 0 else
                              prompt + "\n\nYour previous reply was not valid JSON for the schema. Reply with JSON only.",
                              schema)
            try:
                result = json.loads(text)
                if isinstance(result, dict) and all(k in result for k in schema.get("required", [])):
                    break
                result = None
            except (ValueError, TypeError):
                result = None
        if result is None:
            return dict(UNKNOWN) if "label" in schema["properties"] else {"hook": "", "reason": "invalid_json"}
        if self.s.cache:
            self.db.upsert("llm_cache", ["key"], {"key": key, "response": json.dumps(result),
                                                  "in_tokens": self.in_tokens, "out_tokens": self.out_tokens,
                                                  "created_at": utcnow().isoformat()})
        return result

    def _call(self, prompt: str, schema: dict) -> str:
        self.calls += 1
        resp = self.client.messages.create(
            model=self.s.model,
            max_tokens=self.s.max_tokens,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": prompt}],
        )
        u = getattr(resp, "usage", None)
        if u is not None:
            self.in_tokens += getattr(u, "input_tokens", 0) or 0
            self.out_tokens += getattr(u, "output_tokens", 0) or 0
        if getattr(resp, "stop_reason", None) == "refusal":
            return ""
        return next((b.text for b in resp.content if getattr(b, "type", "") == "text"), "")


def make_llm(settings, db: DB, fixture_mode: bool = False) -> LLM | None:
    """No key, or fixture mode: return None and the pipeline runs rules-only (LLM paths fail closed)."""
    if fixture_mode or not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    return LLM(settings, db)


def accept_llm_claim(claim: dict, store, snap_id: int | None, min_chars: int = 12, text: str | None = None) -> bool:
    """True only if the claimed quote is verbatim (after normalisation) in the snapshot text."""
    if snap_id is None and text is None:
        return False
    body = text if text is not None else store.load_snapshot_text(snap_id)
    return verify_quote(claim.get("quote", "") or "", body, min_chars)
