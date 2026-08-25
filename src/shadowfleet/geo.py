"""Geospatial and time helpers (standard library only)."""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone

EARTH_R_NM = 3440.065  # mean Earth radius in nautical miles


# ---------------------------------------------------------------------------
# Distance / bearing
# ---------------------------------------------------------------------------
def haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in nautical miles. Antimeridian-safe."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2
    return 2 * EARTH_R_NM * math.asin(min(1.0, math.sqrt(a)))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial bearing from point 1 to point 2, degrees true (0-360)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlam = math.radians(lon2 - lon1)
    y = math.sin(dlam) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dlam)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def angle_diff(a: float, b: float) -> float:
    """Signed difference b-a in degrees, normalised to (-180, 180]."""
    d = (b - a + 180.0) % 360.0 - 180.0
    return d + 360.0 if d <= -180.0 else d


def implied_speed_kn(lat1, lon1, t1: datetime, lat2, lon2, t2: datetime) -> float:
    """Average speed in knots required to get from fix 1 to fix 2."""
    hours = (t2 - t1).total_seconds() / 3600.0
    if hours <= 0:
        return float("inf")
    return haversine_nm(lat1, lon1, lat2, lon2) / hours


def in_circle(lat, lon, center_lat, center_lon, radius_nm) -> bool:
    return haversine_nm(lat, lon, center_lat, center_lon) <= radius_nm


# ---------------------------------------------------------------------------
# Time
#
# Every timestamp in this toolkit is stored as UTC at second precision. The
# parsers below exist because each upstream source uses a different format,
# and getting this wrong is invisible: a dropped UTC offset does not raise,
# it silently fabricates AIS gaps and destroys ship-to-ship co-location.
# ---------------------------------------------------------------------------
_SLASH_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})[ T](\d{2}):(\d{2}):(\d{2})$")

# Go's default time layout, which is what aisstream.io emits:
#   2024-05-01 12:00:00.000000000 +0000 UTC
# The 4-digit offset must be space-separated, so this cannot swallow an
# ISO-8601 string carrying a colon offset such as +02:00.
_GO_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})(?:\.(\d+))?"
    r"\s+([+-]\d{4})(?:\s+[A-Za-z/_]{2,10})?$"
)


def _normalise_iso(s: str) -> str:
    """Make a string safe for datetime.fromisoformat across 3.10-3.13."""
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    m = re.search(r"\.(\d+)", s)
    if m and len(m.group(1)) > 6:          # 3.10 rejects >6 fractional digits
        s = s[: m.start() + 7] + s[m.end():]
    return s


def parse_ts(value, dayfirst: bool = True) -> datetime:
    """Parse any timestamp this toolkit encounters into an aware UTC datetime.

    Accepts datetime objects, epoch seconds/milliseconds, ISO-8601 (including
    ``Z`` and ``+HH:MM`` offsets, which are honoured rather than discarded),
    Go/aisstream layout, and the Danish Maritime Authority's slash format.

    ``dayfirst`` controls the reading of ambiguous slash dates. DMA publishes
    day-first, so that is the default; a month field above 12 is detected and
    read the other way round regardless.
    """
    if isinstance(value, datetime):
        return (value.astimezone(timezone.utc) if value.tzinfo
                else value.replace(tzinfo=timezone.utc))
    if isinstance(value, (int, float)):
        secs = value / 1000.0 if abs(value) > 1e11 else float(value)
        return datetime.fromtimestamp(secs, tz=timezone.utc)

    s = str(value).strip()
    if not s:
        raise ValueError("empty timestamp")

    m = _SLASH_RE.match(s)
    if m:
        a, b, year, hh, mm, ss = (int(x) for x in m.groups())
        day, month = (a, b) if dayfirst else (b, a)
        if month > 12 and day <= 12:       # unambiguously the other order
            day, month = month, day
        return datetime(year, month, day, hh, mm, ss, tzinfo=timezone.utc)

    m = _GO_RE.match(s)
    if m:
        date, clock, frac, off = m.groups()
        micro = int((frac or "0").ljust(6, "0")[:6])
        dt = datetime.strptime(f"{date} {clock}", "%Y-%m-%d %H:%M:%S")
        dt = dt.replace(microsecond=micro, tzinfo=timezone.utc)
        sign = 1 if off[0] == "+" else -1
        offset_min = sign * (int(off[1:3]) * 60 + int(off[3:5]))
        return dt - timedelta(minutes=offset_min)

    try:
        dt = datetime.fromisoformat(_normalise_iso(s))
    except ValueError as exc:
        raise ValueError(f"unrecognised timestamp: {value!r}") from exc
    return (dt.astimezone(timezone.utc) if dt.tzinfo
            else dt.replace(tzinfo=timezone.utc))


def iso(dt: datetime) -> str:
    """Canonical storage format: UTC, second precision, trailing Z."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
