"""Storage invariants. Each of these encodes a bug that shipped in v1."""
from __future__ import annotations

import sqlite3

from shadowfleet import db as sfdb


def test_vessel_upsert_dedupes_with_null_imo(conn):
    """v1 keyed on UNIQUE(mmsi, imo, name); SQLite treats NULLs as distinct,
    so the table grew one row per received message."""
    for i in range(200):
        sfdb.upsert_vessel(conn, "273123456", f"2024-05-01T12:00:{i % 60:02d}Z",
                           imo=None, name="NEVA STAR")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM vessels").fetchone()[0] == 1


def test_vessel_upsert_keeps_distinct_identities(conn):
    """Identity churn is the signal; it must not be collapsed away."""
    sfdb.upsert_vessel(conn, "273123456", "2024-05-01T00:00:00Z", name="NEVA STAR")
    sfdb.upsert_vessel(conn, "273123456", "2024-06-01T00:00:00Z", name="POLAR STAR")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM vessels").fetchone()[0] == 2


def test_vessel_first_seen_never_moves_forward(conn):
    sfdb.upsert_vessel(conn, "273123456", "2024-06-01T00:00:00Z", name="X")
    sfdb.upsert_vessel(conn, "273123456", "2024-01-01T00:00:00Z", name="X")
    conn.commit()
    row = conn.execute("SELECT first_seen, last_seen FROM vessels").fetchone()
    assert row["first_seen"] == "2024-01-01T00:00:00Z"
    assert row["last_seen"] == "2024-06-01T00:00:00Z"


def test_distinct_subkinds_coexist(conn):
    """v1's UNIQUE(kind, mmsi, start_ts, counterpart) made two findings about
    one vessel at one instant collide, silently dropping one."""
    for sub in ("multi_mmsi", "bad_mmsi_prefix", "name_churn"):
        sfdb.insert_event(conn, "identity", "273123456", "2024-01-01T00:00:00Z",
                          subkind=sub, severity=50)
    conn.commit()
    assert conn.execute(
        "SELECT COUNT(*) FROM events WHERE kind='identity'").fetchone()[0] == 3


def test_events_still_idempotent(conn):
    for _ in range(3):
        sfdb.insert_event(conn, "gap", "273123456", "2024-01-01T00:00:00Z",
                          severity=50)
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


def test_insert_positions_reports_only_new_rows(conn):
    rows = [{"mmsi": "1", "ts": f"2024-01-01T00:00:{i:02d}Z", "lat": 1.0,
             "lon": 1.0, "sog": 1.0, "cog": None, "heading": None,
             "nav_status": 0, "draught": None, "source": "t"} for i in range(10)]
    assert sfdb.insert_positions(conn, rows) == 10
    assert sfdb.insert_positions(conn, rows) == 0


def test_migration_from_v1_schema(tmp_path):
    """A v1 database must upgrade in place and collapse duplicated vessels."""
    path = str(tmp_path / "v1.db")
    raw = sqlite3.connect(path)
    raw.executescript("""
        CREATE TABLE positions (id INTEGER PRIMARY KEY, mmsi TEXT NOT NULL,
            ts TEXT NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL, sog REAL,
            cog REAL, heading REAL, nav_status INTEGER, draught REAL,
            source TEXT, UNIQUE(mmsi, ts));
        CREATE TABLE vessels (mmsi TEXT NOT NULL, imo TEXT, name TEXT,
            callsign TEXT, ship_type INTEGER, flag TEXT, length_m REAL,
            width_m REAL, first_seen TEXT, last_seen TEXT,
            UNIQUE(mmsi, imo, name));
        CREATE TABLE designations (imo TEXT NOT NULL, name TEXT,
            authority TEXT NOT NULL, program TEXT, listed_on TEXT, source TEXT,
            fetched_at TEXT, raw TEXT, PRIMARY KEY (imo, authority));
        CREATE TABLE events (id INTEGER PRIMARY KEY, kind TEXT NOT NULL,
            mmsi TEXT NOT NULL, imo TEXT, name TEXT, start_ts TEXT NOT NULL,
            end_ts TEXT, lat REAL, lon REAL, lat2 REAL, lon2 REAL,
            counterpart TEXT NOT NULL DEFAULT '', zone TEXT,
            severity REAL DEFAULT 0, detail TEXT,
            UNIQUE(kind, mmsi, start_ts, counterpart));
        CREATE TABLE risk (mmsi TEXT PRIMARY KEY, imo TEXT, name TEXT,
            flag TEXT, score REAL, band TEXT, factors TEXT, scored_at TEXT);
    """)
    raw.execute("INSERT INTO positions (mmsi, ts, lat, lon) VALUES ('1','t',0,0)")
    for i in range(50):                       # the v1 explosion
        raw.execute("INSERT INTO vessels (mmsi, imo, name, first_seen, last_seen)"
                    " VALUES ('273123456', NULL, 'NEVA STAR', ?, ?)",
                    (f"2024-01-01T00:00:{i:02d}Z", f"2024-01-01T00:00:{i:02d}Z"))
    raw.execute("INSERT INTO events (kind, mmsi, start_ts) VALUES ('gap','1','t')")
    raw.commit()
    raw.close()

    conn = sfdb.connect(path, verbose=False)
    assert conn.execute("SELECT COUNT(*) FROM vessels").fetchone()[0] == 1
    assert conn.execute("PRAGMA user_version").fetchone()[0] == sfdb.SCHEMA_VERSION
    assert "subkind" in {r[1] for r in conn.execute("PRAGMA table_info(events)")}
    assert "basis" in {r[1] for r in conn.execute("PRAGMA table_info(designations)")}
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    conn.close()
