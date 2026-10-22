"""`receipts stats`: counts and acceptance-rule checks (A1, A2, A5, A6)."""
from __future__ import annotations

from dataclasses import dataclass, field

from receipts.db import DB
from receipts.origin import candidate_is_live, creator_is_live


@dataclass
class Rule:
    code: str
    desc: str
    ok: bool
    value: str


@dataclass
class Stats:
    qualified: int = 0
    reserve: int = 0
    by_segment: dict[str, int] = field(default_factory=dict)
    by_country: dict[str, int] = field(default_factory=dict)
    priority: dict[str, int] = field(default_factory=dict)
    priority_a_full: int = 0
    dupes: dict[str, int] = field(default_factory=dict)
    messages_validated: int = 0
    messages_total: int = 0
    channel_mix: dict[str, int] = field(default_factory=dict)
    rejected: int = 0
    reject_reasons: dict[str, int] = field(default_factory=dict)
    runs: list[dict] = field(default_factory=list)
    rules: list[Rule] = field(default_factory=list)


SEL_ALL = "FROM leads l JOIN creators c USING(creator_key) WHERE l.status='Qualified' AND c.status='qualified'"


def compute(db: DB, s, live_only: bool = True) -> Stats:
    """Acceptance stats. live_only (default): only creators whose every receipt came from a live fetch,
    and only candidates/runs from live runs; demo, fixture and synthetic data are excluded."""
    SEL = SEL_ALL + (f" AND {creator_is_live('c')}" if live_only else "")
    cand = f" AND {candidate_is_live('ca')}" if live_only else ""
    st = Stats()
    st.qualified = db.scalar(f"SELECT COUNT(*) {SEL}") or 0
    st.reserve = db.scalar("SELECT COUNT(*) FROM leads l JOIN creators c USING(creator_key) WHERE l.status='Reserve'"
                           + (f" AND {creator_is_live('c')}" if live_only else "")) or 0
    st.by_segment = {r[0] or "?": r[1] for r in db.fetch_all(f"SELECT c.segment, COUNT(*) {SEL} GROUP BY c.segment")}
    st.by_country = {r[0] or "?": r[1] for r in db.fetch_all(
        f"SELECT c.country_iso2, COUNT(*) n {SEL} GROUP BY c.country_iso2 ORDER BY n DESC")}
    st.priority = {r[0]: r[1] for r in db.fetch_all(f"SELECT l.priority, COUNT(*) {SEL} GROUP BY l.priority")}
    st.priority_a_full = db.scalar(
        f"SELECT COUNT(*) {SEL} AND l.priority='A' AND (SELECT COUNT(*) FROM messages m WHERE m.creator_key=c.creator_key "
        "AND m.validated=1) >= 3") or 0
    # duplicates by canonical key and by every identity-ish column among selected leads
    st.dupes = {
        "creator_key": db.scalar(f"SELECT COUNT(*) - COUNT(DISTINCT c.creator_key) {SEL}") or 0,
        "stable_id": db.scalar(f"SELECT COUNT(*) - COUNT(DISTINCT c.primary_id) {SEL}") or 0,
        "website_domain": db.scalar(f"SELECT COUNT(c.website_domain) - COUNT(DISTINCT c.website_domain) {SEL}") or 0,
        "profile_url": db.scalar(f"SELECT COUNT(c.profile_url) - COUNT(DISTINCT lower(c.profile_url)) {SEL}") or 0,
        "identity_shared": db.scalar(
            "SELECT COUNT(*) FROM (SELECT i.platform, i.platform_id FROM identities i JOIN leads l USING(creator_key) "
            f"WHERE l.status='Qualified' AND i.creator_key IN (SELECT c.creator_key {SEL}) "
            "GROUP BY i.platform, i.platform_id HAVING COUNT(DISTINCT i.creator_key) > 1)") or 0,
    }
    st.messages_total = db.scalar(f"SELECT COUNT(*) FROM messages WHERE creator_key IN (SELECT c.creator_key {SEL})") or 0
    st.messages_validated = db.scalar(
        f"SELECT COUNT(*) FROM messages WHERE validated=1 AND creator_key IN (SELECT c.creator_key {SEL})") or 0
    st.channel_mix = {r[0]: r[1] for r in db.fetch_all(
        "SELECT ch.channel_type, COUNT(*) n FROM channels ch "
        f"WHERE ch.role = 'best' AND ch.creator_key IN (SELECT c.creator_key {SEL}) GROUP BY ch.channel_type ORDER BY n DESC")}
    st.rejected = db.scalar(f"SELECT COUNT(*) FROM candidates ca WHERE ca.status='rejected'{cand}") or 0
    st.reject_reasons = {r[0]: r[1] for r in db.fetch_all(
        f"SELECT reject_reason, COUNT(*) n FROM candidates ca WHERE ca.status='rejected'{cand} "
        "GROUP BY reject_reason ORDER BY n DESC")}
    st.runs = [dict(r) for r in db.fetch_all(
        "SELECT * FROM runs" + (" WHERE data_origin = 'live'" if live_only else "") + " ORDER BY run_id")]
    st.rules = rules(st, s)
    return st


def balance_rules(st: Stats, s) -> list[Rule]:
    n = st.qualified or 1
    approved = set(s.countries.get("approved", []))
    in_approved = sum(v for k, v in st.by_country.items() if k in approved) / n
    top = max(st.by_country.values(), default=0) / n
    return [
        Rule("A5a", "at least 80% from approved countries", st.qualified > 0 and in_approved >= 0.80, f"{in_approved:.0%}"),
        Rule("A5b", "at least 3 countries", len(st.by_country) >= 3, str(len(st.by_country))),
        Rule("A5c", "no country above 60%", st.qualified > 0 and top <= 0.60, f"{top:.0%}"),
    ]


def rules(st: Stats, s) -> list[Rule]:
    total_dupes = sum(st.dupes.values())
    return [
        Rule("A1", "at least 1,000 qualified leads", st.qualified >= 1000, str(st.qualified)),
        Rule("A2", "0 duplicates by canonical key", total_dupes == 0, str(total_dupes)),
        *balance_rules(st, s),
        Rule("A6", f"at least {max(25, s.priority.target_count)} Priority A with 3 validated messages",
             st.priority_a_full >= max(25, s.priority.target_count), str(st.priority_a_full)),
    ]
