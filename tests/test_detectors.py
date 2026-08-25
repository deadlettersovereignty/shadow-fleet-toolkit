"""Detector behaviour against the synthetic fleet.

The fleet plants exactly one instance of each behaviour plus a control. The
control assertions matter as much as the positive ones: a detector that fires
on everything is worse than useless in an investigative context.
"""
from __future__ import annotations

import json
import math

import pytest

from shadowfleet import db as sfdb
from shadowfleet import risk
from shadowfleet.detect_sts import build_grid, cell_of, find_contacts
from shadowfleet.geo import haversine_nm, iso
from shadowfleet.testdata import FLEET

ALPHA, BRAVO, CHARLIE, DELTA, ECHO, FOXTROT = (f[0] for f in FLEET)


def kinds_for(conn, mmsi):
    return {r["kind"] for r in conn.execute(
        "SELECT DISTINCT kind FROM events WHERE mmsi=?", (mmsi,))}


# --- positives -------------------------------------------------------------
def test_alpha_dark_gap(fleet):
    row = fleet.execute(
        "SELECT * FROM events WHERE kind='gap' AND mmsi=?", (ALPHA,)).fetchone()
    assert row is not None
    assert json.loads(row["detail"])["hours"] > 24
    assert row["severity"] > 50


def test_delta_teleport(fleet):
    row = fleet.execute(
        "SELECT * FROM events WHERE kind='teleport' AND mmsi=?", (DELTA,)).fetchone()
    assert row is not None
    assert json.loads(row["detail"])["implied_speed_kn"] > 100


def test_echo_circular_track(fleet):
    assert "circle" in kinds_for(fleet, ECHO)


def test_alpha_identity_reuse_detected(fleet):
    subs = {r["subkind"] for r in fleet.execute(
        "SELECT subkind FROM events WHERE kind='identity' AND mmsi=?", (ALPHA,))}
    assert "multi_mmsi" in subs


# --- the two-sided STS fix -------------------------------------------------
def test_sts_recorded_for_both_parties(fleet):
    """v1 wrote the encounter only against vessel A, so the counterparty was
    never credited and scored as though nothing had happened."""
    for mmsi in (BRAVO, CHARLIE):
        assert "sts" in kinds_for(fleet, mmsi), f"{mmsi} missing its STS record"


def test_sts_counterparts_point_at_each_other(fleet):
    rows = fleet.execute(
        "SELECT mmsi, counterpart FROM events WHERE kind='sts'").fetchall()
    pairs = {(r["mmsi"], r["counterpart"]) for r in rows}
    assert (BRAVO, CHARLIE) in pairs and (CHARLIE, BRAVO) in pairs


def test_mirror_row_is_marked(fleet):
    """Reports must be able to exclude the reciprocal row to avoid double-plotting."""
    subs = {r["subkind"] for r in fleet.execute(
        "SELECT subkind FROM events WHERE kind='sts'")}
    assert subs == {"", "mirror"}


# --- the control -----------------------------------------------------------
def test_control_vessel_is_clean(fleet):
    assert kinds_for(fleet, FOXTROT) == set()


def test_control_vessel_scores_zero(fleet, tmp_path):
    rec = risk.score_vessel(fleet, FOXTROT, [], {}, "1970-01-01T00:00:00Z")
    assert rec["score"] == 0, f"control scored on: {rec['factors']}"


# --- STS grid geometry -----------------------------------------------------
@pytest.mark.parametrize("max_dist,lat", [(0.5, 0), (0.5, 60), (2.0, 60),
                                          (2.0, 78), (5.0, 71)])
def test_grid_cell_always_spans_the_search_radius(max_dist, lat):
    """A fixed cell size silently missed pairs once the radius was widened."""
    cell_lat, cell_lon, n_lon = build_grid(max_dist, lat)
    assert cell_lat * 60 >= max_dist - 1e-9
    assert cell_lon * 60 * math.cos(math.radians(lat)) >= max_dist - 1e-9
    assert n_lon >= 1


def test_wide_radius_pair_is_found(tmp_path):
    """Regression: two vessels 1.9 nm apart with --max-distance 2.0 landed two
    cells apart under the old fixed grid and were reported as no rendezvous."""
    from datetime import datetime, timedelta, timezone

    path = str(tmp_path / "grid.db")
    conn = sfdb.connect(path, verbose=False)
    lat, lon_a = 60.0, 28.0499          # deliberately at the top of a v1 cell
    lon_b = lon_a + 1.9 / 60 / math.cos(math.radians(lat))
    assert haversine_nm(lat, lon_a, lat, lon_b) == pytest.approx(1.9, rel=1e-2)

    t0 = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = []
    for i in range(30):
        for mmsi, lon in (("538999901", lon_a), ("538999902", lon_b)):
            rows.append({"mmsi": mmsi, "ts": iso(t0 + timedelta(minutes=6 * i)),
                         "lat": lat, "lon": lon, "sog": 0.2, "cog": None,
                         "heading": None, "nav_status": 1, "draught": None,
                         "source": "t"})
    sfdb.insert_positions(conn, rows)

    contacts, _, _, _ = find_contacts(conn, None, None, 600, 1.5, 2.0)
    assert contacts, "pair within max-distance was not detected"
    conn.close()


def test_antimeridian_cells_are_adjacent():
    cell_lat, cell_lon, n_lon = build_grid(0.5, 50)
    east = cell_of(50, 179.99, cell_lat, cell_lon, n_lon)
    west = cell_of(50, -179.99, cell_lat, cell_lon, n_lon)
    assert abs(east[1] - west[1]) % n_lon <= 1
