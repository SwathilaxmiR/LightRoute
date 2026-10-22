"""Date helpers. One clock (utcnow) so tests can freeze time."""
from __future__ import annotations

import re
import statistics
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

_DUR = re.compile(
    r"^P(?:(?P<w>\d+)W)?(?:(?P<d>\d+)D)?(?:T(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?(?:(?P<s>\d+(?:\.\d+)?)S)?)?$"
)


def parse_iso_duration(s: str) -> int:
    """'PT1H2M3S' -> 3723; 'P0D' (live streams) -> 0. Unparseable -> 0."""
    m = _DUR.match((s or "").strip())
    if not m:
        return 0
    g = {k: float(v) if v else 0.0 for k, v in m.groupdict().items()}
    return int(g["w"] * 604800 + g["d"] * 86400 + g["h"] * 3600 + g["m"] * 60 + g["s"])


def parse_hms(s: str | int | None) -> int | None:
    """itunes:duration: 'HH:MM:SS', 'MM:SS' or plain seconds."""
    if s is None or s == "":
        return None
    if isinstance(s, int):
        return s
    s = str(s).strip()
    try:
        if ":" in s:
            parts = [int(float(p)) for p in s.split(":")]
            secs = 0
            for p in parts:
                secs = secs * 60 + p
            return secs
        return int(float(s))
    except ValueError:
        return None


def utcnow() -> datetime:
    return datetime.now(UTC)


def parse_dt(value: str | datetime | None) -> datetime | None:
    """ISO 8601 or RFC 2822 -> aware UTC datetime."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        v = str(value).strip()
        try:
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            try:
                dt = parsedate_to_datetime(v)
            except (TypeError, ValueError):
                return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def within_days(dt: datetime, days: int, now: datetime | None = None) -> bool:
    """Inclusive: an item exactly `days` days old is inside the window."""
    now = now or utcnow()
    return now - timedelta(days=days) <= dt <= now + timedelta(minutes=5)


def _gaps(dates: list[datetime]) -> list[float]:
    ds = sorted(dates)
    return [(b - a).total_seconds() / 86400 for a, b in zip(ds, ds[1:], strict=False)]


def median_gap_days(dates: list[datetime]) -> float | None:
    g = _gaps(dates)
    return statistics.median(g) if g else None


def max_gap_days(dates: list[datetime]) -> float | None:
    g = _gaps(dates)
    return max(g) if g else None


def ymd(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%d")


def date_words(dt: datetime) -> str:
    """'14 October' style, no year, for message copy."""
    return f"{dt.day} {dt.strftime('%B')}"
