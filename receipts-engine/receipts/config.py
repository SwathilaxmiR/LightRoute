"""Load and validate config/*.yaml into a pydantic Settings object.

Config directory resolution: $RECEIPTS_CONFIG_DIR, else ./config.
Path overrides for demo/bench/tests: $RECEIPTS_DB, $RECEIPTS_OUT.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Project(_M):
    user_agent: str
    timezone: str = "UTC"


class Sender(_M):
    name: str
    role: str
    postal_address: str
    reply_to: str


class Http(_M):
    max_concurrency: int
    per_host_delay_s: float
    timeout_s: float
    retries: int
    backoff_base_s: float
    circuit_breaker_failures: int
    cache_hours: float = 24


class YouTubeCost(_M):
    search_list: int
    channels_list: int
    playlist_items_list: int
    videos_list: int
    comment_threads_list: int


class Quota(_M):
    youtube_daily_units: int
    youtube_stop_at_fraction: float
    youtube_cost: YouTubeCost


class Thresholds(_M):
    active_count_30d: int
    active_longform_60d: int
    longform_min_seconds: int
    short_max_seconds: int
    window_days_short: int
    window_days_long: int
    language_items: int
    language_min_english: int
    need_min_signals: int
    need_min_strong: int
    cadence_gap_multiple: float
    backlog_min_items: int
    caption_missing_fraction: float
    caption_sample_size: int
    fit_followers_min: int
    fit_followers_max: int
    max_followers_hard: int
    podcast_min_episodes: int
    podcast_min_median_minutes: float
    reupload_duration_tolerance_s: int = 2


class Freshness(_M):
    max_evidence_age_days: int
    ttl_refresh_days: int
    final_recheck: bool


class QuotasFinal(_M):
    max_country_share: float
    min_countries: int
    min_country_share_each: float
    segment_targets: dict[str, float]
    segment_tolerance: float


class Priority(_M):
    target_count: int
    require_manual_check_for: list[str]


class Scoring(_M):
    weights: dict[str, float]
    need_strong_points: float
    need_medium_points: float


class LLMSettings(_M):
    provider: str
    model: str
    max_tokens: int
    temperature: float
    cache: bool
    usd_per_mtok_in: float
    usd_per_mtok_out: float
    max_rewrite_attempts: int


class MessageLimits(_M):
    email_words_first: int
    email_words_followup: int
    email_words_close: int
    linkedin_chars_first: int
    form_words_first: int


class SeedTest(_M):
    allowed_recipients: list[str]
    tracker_base_url: str
    daily_caps: dict[str, int]
    time_compression_factor: float
    redirect_target: str = "https://example.com/"
    community_webhook_env: str = "COMMUNITY_WEBHOOK_URL"


class Bench(_M):
    rows: int
    db_path: str


class Paths(_M):
    db: str
    out_dir: str
    snapshots_dir: str
    fixtures_dir: str


class Workbook(_M):
    path: str = ""
    yellow_rgbs: list[str]
    yellow_other: list[str] = []
    rows_max: int


class Discovery(_M):
    snowball_depth: int
    podcast_max_age_days: int
    podcast_language_prefix: str


class TextCfg(_M):
    quote_min_chars: int
    lang_min_confidence: float = 0.1
    lang_min_letters: int = 10


class Identity(_M):
    fuzzy_review_ratio: float
    handle_name_match_ratio: float


class Evidence(_M):
    sponsorship_window_days: int
    backlog_old_item_days: int
    cadence_window_days: int
    cadence_compare_days: int
    captions_gap_strength: str
    shorts_conservative_max_seconds: int = 180
    team_workflow_pattern: str = r"edited by|editor:|producer:|produced by|hiring.{0,40}editor|video editor wanted"


class Channels(_M):
    business_words: list[str]
    weight_business_language: float
    weight_creator_owned: float
    weight_automatable: float
    responsiveness_cap: float
    reply_rate_plus1: float
    reply_rate_plus2: float
    active_window_days: int
    assisted_types: list[str]


class FitSize(_M):
    taper_floor: float
    unknown_value: float


class PrioritySplit(_M):
    b_fraction: float
    min_proof_receipts: int


class Outreach(_M):
    opt_out_line: str
    linkedin_opt_out: str
    hook_max_words: int
    hook_recent_days: int


class SegmentCfg(_M):
    expert_min_items_60d: int = 4


class PipelineCfg(_M):
    candidate_concurrency: int = 4


class Audit(_M):
    metric_tolerance: float


class Settings(_M):
    project: Project
    sender: Sender
    offer_line: str
    http: Http
    quota: Quota
    thresholds: Thresholds
    freshness: Freshness
    quotas_final: QuotasFinal
    priority: Priority
    scoring: Scoring
    llm: LLMSettings
    message_limits: MessageLimits
    follow_up_days: list[int]
    seed_test: SeedTest
    bench: Bench
    paths: Paths
    workbook: Workbook
    discovery: Discovery
    text: TextCfg
    identity: Identity
    evidence: Evidence
    channels: Channels
    fit_size: FitSize
    priority_split: PrioritySplit
    outreach: Outreach
    audit: Audit
    segment: SegmentCfg = SegmentCfg()
    pipeline: PipelineCfg = PipelineCfg()

    # side files
    seeds: dict[str, Any] = Field(default_factory=dict)
    countries: dict[str, Any] = Field(default_factory=dict)
    exclusions: dict[str, Any] = Field(default_factory=dict)
    monetization: dict[str, Any] = Field(default_factory=dict)
    banned: dict[str, Any] = Field(default_factory=dict)

    config_dir: str = "config"

    # ---- convenience -------------------------------------------------------
    @property
    def db_path(self) -> Path:
        return Path(os.environ.get("RECEIPTS_DB") or self.paths.db)

    @property
    def out_dir(self) -> Path:
        return Path(os.environ.get("RECEIPTS_OUT") or self.paths.out_dir)

    @property
    def snapshots_dir(self) -> Path:
        if os.environ.get("RECEIPTS_OUT"):
            return self.out_dir / "snapshots"
        return Path(self.paths.snapshots_dir)

    @property
    def docs_dir(self) -> Path:
        """docs/ for real runs; demo/test/bench runs (RECEIPTS_OUT set) keep their reports beside their output."""
        if os.environ.get("RECEIPTS_DOCS"):
            return Path(os.environ["RECEIPTS_DOCS"])
        return self.out_dir / "docs" if os.environ.get("RECEIPTS_OUT") else Path("docs")

    @property
    def fixtures_dir(self) -> Path:
        return Path(os.environ.get("RECEIPTS_FIXTURES") or self.paths.fixtures_dir)


def config_dir() -> Path:
    return Path(os.environ.get("RECEIPTS_CONFIG_DIR") or "config")


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_settings(cfg_dir: str | Path | None = None, overrides: dict | None = None) -> Settings:
    load_dotenv(override=False)
    d = Path(cfg_dir) if cfg_dir else config_dir()
    data = _read_yaml(d / "settings.yaml")
    if overrides:
        data = _deep_merge(data, overrides)
    data["seeds"] = _read_yaml(d / "seeds.yaml")
    data["countries"] = _read_yaml(d / "countries.yaml")
    data["exclusions"] = _read_yaml(d / "exclusions.yaml")
    data["monetization"] = _read_yaml(d / "monetization_domains.yaml")
    data["banned"] = _read_yaml(d / "banned_phrases.yaml")
    data["config_dir"] = str(d)
    return Settings.model_validate(data)


def _deep_merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()


def fixture_mode() -> bool:
    return os.environ.get("FIXTURE_MODE", "0") == "1"
