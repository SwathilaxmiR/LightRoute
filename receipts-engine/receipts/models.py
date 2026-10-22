"""Pydantic v2 models (BUILD_SPEC section 7). All datetimes are timezone-aware UTC."""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class Segment(StrEnum):
    podcaster = "podcaster"
    youtuber = "youtuber"
    expert_led = "expert_led"


SEGMENT_DISPLAY = {
    Segment.podcaster: "Podcaster",
    Segment.youtuber: "YouTuber",
    Segment.expert_led: "Expert-led creator",
}


class Channel(StrEnum):
    email = "email"
    form = "form"
    linkedin = "linkedin"
    instagram = "instagram"
    x = "x"
    youtube = "youtube"
    website = "website"


CHANNEL_DISPLAY = {
    Channel.email: "Email",
    Channel.form: "Contact form",
    Channel.linkedin: "LinkedIn",
    Channel.instagram: "Instagram",
    Channel.x: "X",
    Channel.youtube: "YouTube",
    Channel.website: "Website",
}

PLATFORM_DISPLAY = {"youtube": "YouTube", "podcast": "Podcast", "website": "Website"}

Platform = Literal["youtube", "podcast", "website"]
Result = Literal["pass", "fail", "na"]
VerifiedBy = Literal["rule", "llm_verified", "manual"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Candidate(_Strict):
    platform: Platform
    source_id: str
    url: str
    source: str
    hint: dict[str, Any] = Field(default_factory=dict)


class Snapshot(_Strict):
    snap_id: int
    url: str
    fetched_at: AwareDatetime
    status_code: int | None
    sha256: str
    path: str
    data_origin: Literal["live", "fixture", "synthetic"]


class ContentItem(_Strict):
    platform: str
    item_id: str
    url: str
    title: str | None = None
    published_at: AwareDatetime
    duration_s: int | None = None
    is_short: bool = False
    has_captions: bool | None = None
    paid_placement: bool | None = None
    description: str | None = None
    snap_id: int | None = None


class CollectedCreator(BaseModel):
    candidate: Candidate
    profile: dict[str, Any] = Field(default_factory=dict)
    items: list[ContentItem] = Field(default_factory=list)
    snap_ids: list[int] = Field(default_factory=list)
    website_pages: dict[str, tuple[int, str]] = Field(default_factory=dict)
    feed_meta: dict[str, Any] | None = None


class CheckResult(_Strict):
    check_name: str
    sub_key: str = ""
    result: Result
    evidence_url: str | None = None
    quote_or_metric: str | None = None
    snap_id: int | None = None
    verified_by: VerifiedBy = "rule"
    reason_code: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.result == "pass"


class ChannelCandidate(_Strict):
    channel_type: Channel
    handle_or_url: str
    source_url: str
    source_quote: str
    snap_id: int | None = None
    business_language: bool
    live_ok: bool
    name_match: bool
    mx_ok: bool | None = None
    last_active: str | None = None
    active_proof: Literal["api", "feed", "site_date", "manual"] | None = None


LEAD_COLUMNS = [
    "lead_id", "creator_name", "creator_segment", "country_iso2", "primary_language",
    "primary_platform", "profile_url", "stable_creator_id", "followers_or_subscribers",
    "last_content_date", "recent_content_url", "content_count_30d", "longform_count_60d",
    "monetization_signal", "monetization_evidence_url", "incubrix_need", "need_evidence_url",
    "best_channel", "channel_handle_or_url", "channel_evidence_url", "backup_channel",
    "personalization_hook", "first_message", "checked_date", "priority", "lead_score",
    "candidate_status", "rejection_reason", "duplicate_check", "completeness_check",
]

# rejection_reason is legitimately empty for qualified rows
LEAD_OPTIONAL = {"rejection_reason"}


class Lead(_Strict):
    lead_id: str
    creator_name: str
    creator_segment: Literal["Podcaster", "YouTuber", "Expert-led creator"]
    country_iso2: str
    primary_language: Literal["en"]
    primary_platform: Literal["YouTube", "Podcast", "Website"]
    profile_url: str
    stable_creator_id: str
    followers_or_subscribers: str
    last_content_date: str
    recent_content_url: str
    content_count_30d: int
    longform_count_60d: int
    monetization_signal: str
    monetization_evidence_url: str
    incubrix_need: str
    need_evidence_url: str
    best_channel: str
    channel_handle_or_url: str
    channel_evidence_url: str
    backup_channel: str
    personalization_hook: str
    first_message: str
    checked_date: str
    priority: Literal["A", "B", "C"]
    lead_score: str
    candidate_status: Literal["Qualified"] = "Qualified"
    rejection_reason: str = ""
    duplicate_check: Literal["UNIQUE"] = "UNIQUE"
    completeness_check: Literal["COMPLETE"] = "COMPLETE"

    def row(self) -> list[str]:
        d = self.model_dump()
        return [str(d[c]) for c in LEAD_COLUMNS]


class Message(_Strict):
    channel: Literal["email", "linkedin", "form"]
    step: int
    subject: str | None = None
    body: str
    hook: str
    hook_source_url: str


def to_iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")
