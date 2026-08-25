"""Timestamp parsing. A dropped UTC offset does not raise - it silently
fabricates AIS gaps - so every accepted format is pinned here."""
from __future__ import annotations

import pytest

from shadowfleet.geo import angle_diff, bearing_deg, haversine_nm, iso, parse_ts


@pytest.mark.parametrize("value,expected", [
    ("2024-05-01T12:00:00Z",                     "2024-05-01T12:00:00Z"),
    ("2024-05-01T12:00:00+00:00",                "2024-05-01T12:00:00Z"),
    ("2024-05-01T12:00:00+02:00",                "2024-05-01T10:00:00Z"),
    ("2024-05-01T12:00:00-05:00",                "2024-05-01T17:00:00Z"),
    ("2024-05-01T12:00:00.123456789Z",           "2024-05-01T12:00:00Z"),
    ("2024-05-01 12:00:00.000000000 +0000 UTC",  "2024-05-01T12:00:00Z"),
    ("2024-05-01 12:00:00.000000000 +0200 UTC",  "2024-05-01T10:00:00Z"),
    ("01/05/2024 12:00:00",                      "2024-05-01T12:00:00Z"),
    ("31/12/2024 23:00:00",                      "2024-12-31T23:00:00Z"),
    (1714564800,                                 "2024-05-01T12:00:00Z"),
    (1714564800000,                              "2024-05-01T12:00:00Z"),
])
def test_parse_ts_formats(value, expected):
    assert iso(parse_ts(value)) == expected


def test_iso_offset_is_applied_not_discarded():
    """Regression: the aisstream regex used to swallow ISO colon offsets."""
    naive = parse_ts("2024-05-01T12:00:00Z")
    shifted = parse_ts("2024-05-01T12:00:00+02:00")
    assert (naive - shifted).total_seconds() == 2 * 3600


def test_slash_date_month_first_detected():
    assert iso(parse_ts("05/31/2024 01:00:00")) == "2024-05-31T01:00:00Z"


def test_slash_date_respects_dayfirst_flag():
    assert iso(parse_ts("01/05/2024 00:00:00", dayfirst=False)) == "2024-01-05T00:00:00Z"


def test_rejects_garbage():
    with pytest.raises(ValueError):
        parse_ts("not a timestamp")


def test_haversine_and_bearing():
    assert haversine_nm(0, 0, 0, 1) == pytest.approx(60.0, rel=1e-3)
    assert bearing_deg(0, 0, 1, 0) == pytest.approx(0.0, abs=1e-6)
    # antimeridian must not be treated as half a world away
    assert haversine_nm(50, 179.99, 50, -179.99) < 1.0


def test_angle_diff_wraps():
    assert angle_diff(350, 10) == pytest.approx(20.0)
    assert angle_diff(10, 350) == pytest.approx(-20.0)
