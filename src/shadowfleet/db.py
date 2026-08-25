"""SQLite storage layer shared by every stage of the pipeline.

Schema notes that matter:

* ``vessels`` is uniquely indexed on ``(mmsi, IFNULL(imo,''), IFNULL(name,''))``
  rather than a plain ``UNIQUE(mmsi, imo, name)``. SQLite treats NULLs as
  distinct inside a unique constraint, so the plain form never fires when the
  IMO is unknown and the table grows one row per received message.
* ``events`` includes ``subkind`` in its uniqueness key. Without it, two
  different findings about the same vessel at the same instant collide and one
  is silently discarded by ``INSERT OR IGNORE``.
* ``designations.basis`` records whether a vessel was listed in its own right
  (``direct``) or merely mentioned in some other entity's remarks (``linked``).
  Conflating the two puts undesignated hulls on a sanctions list.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

DEFAULT_DB = "shadowfleet.db"
SCHEMA_VERSION = 2

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS positions (
    id         INTEGER PRIMARY KEY,
    mmsi       TEXT NOT NULL,
    ts         TEXT NOT NULL,        -- ISO8601 UTC, second precision
    lat        REAL NOT NULL,
    lon        REAL NOT NULL,
    sog        REAL,                 -- speed over ground, knots
    cog        REAL,
    heading    REAL,
    nav_status INTEGER,
    draught    REAL,
    source     TEXT,
    UNIQUE(mmsi, ts)
);
CREATE INDEX IF NOT EXISTS idx_pos_mmsi_ts ON positions(mmsi, ts);
CREATE INDEX IF NOT EXISTS idx_pos_ts      ON positions(ts);

CREATE TABLE IF NOT EXISTS vessels (
    mmsi       TEXT NOT NULL,
    imo        TEXT,
    name       TEXT,
    callsign   TEXT,
    ship_type  INTEGER,
    flag       TEXT,
    length_m   REAL,
    width_m    REAL,
    first_seen TEXT,
    last_seen  TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_vessels_identity
    ON vessels(mmsi, IFNULL(imo,''), IFNULL(name,''));
CREATE INDEX IF NOT EXISTS idx_vessels_imo ON vessels(imo);

CREATE TABLE IF NOT EXISTS designations (
    imo        TEXT NOT NULL,
    name       TEXT,
    authority  TEXT NOT NULL,        -- OFAC | EU | UK | OTHER
    basis      TEXT NOT NULL DEFAULT 'direct',  -- direct | linked | unverified
    program    TEXT,
    listed_on  TEXT,
    source     TEXT,
    fetched_at TEXT,
    raw        TEXT,
    PRIMARY KEY (imo, authority)
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,       -- gap|sts|teleport|frozen|circle|identity
    subkind     TEXT NOT NULL DEFAULT '',
    mmsi        TEXT NOT NULL,
    imo         TEXT,
    name        TEXT,
    start_ts    TEXT NOT NULL,
    end_ts      TEXT,
    lat         REAL,
    lon         REAL,
    lat2        REAL,
    lon2        REAL,
    counterpart TEXT NOT NULL DEFAULT '',
    zone        TEXT,
    severity    REAL DEFAULT 0,
    detail      TEXT,                -- JSON blob
    UNIQUE(kind, subkind, mmsi, start_ts, counterpart)
);
CREATE INDEX IF NOT EXISTS idx_events_mmsi ON events(mmsi);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind);

CREATE TABLE IF NOT EXISTS risk (
    mmsi      TEXT PRIMARY KEY,
    imo       TEXT,
    name      TEXT,
    flag      TEXT,
    score     REAL,
    band      TEXT,
    factors   TEXT,                  -- JSON list of {code, points, note}
    scored_at TEXT
);
"""


# ---------------------------------------------------------------------------
# Connection + migration
# ---------------------------------------------------------------------------
def _table_exists(conn, name) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _columns(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _migrate_v1(conn) -> list:
    """Upgrade a v1 database in place. Returns a list of human-readable notes."""
    notes = []

    if _table_exists(conn, "vessels") and "idx_vessels_identity" not in {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}:
        before = conn.execute("SELECT COUNT(*) FROM vessels").fetchone()[0]
        conn.executescript("""
            ALTER TABLE vessels RENAME TO vessels_v1;
            CREATE TABLE vessels (
                mmsi TEXT NOT NULL, imo TEXT, name TEXT, callsign TEXT,
                ship_type INTEGER, flag TEXT, length_m REAL, width_m REAL,
                first_seen TEXT, last_seen TEXT);
            INSERT INTO vessels
              SELECT mmsi, imo, name, MAX(callsign), MAX(ship_type), MAX(flag),
                     MAX(length_m), MAX(width_m), MIN(first_seen), MAX(last_seen)
              FROM vessels_v1
              GROUP BY mmsi, IFNULL(imo,''), IFNULL(name,'');
            DROP TABLE vessels_v1;
        """)
        after = conn.execute("SELECT COUNT(*) FROM vessels").fetchone()[0]
        if before != after:
            notes.append(f"vessels: collapsed {before:,} duplicate rows to {after:,}")

    if _table_exists(conn, "events") and "subkind" not in _columns(conn, "events"):
        conn.executescript("""
            ALTER TABLE events RENAME TO events_v1;
            CREATE TABLE events (
                id INTEGER PRIMARY KEY, kind TEXT NOT NULL,
                subkind TEXT NOT NULL DEFAULT '', mmsi TEXT NOT NULL,
                imo TEXT, name TEXT, start_ts TEXT NOT NULL, end_ts TEXT,
                lat REAL, lon REAL, lat2 REAL, lon2 REAL,
                counterpart TEXT NOT NULL DEFAULT '', zone TEXT,
                severity REAL DEFAULT 0, detail TEXT,
                UNIQUE(kind, subkind, mmsi, start_ts, counterpart));
            INSERT INTO events (kind, mmsi, imo, name, start_ts, end_ts, lat, lon,
                                lat2, lon2, counterpart, zone, severity, detail)
              SELECT kind, mmsi, imo, name, start_ts, end_ts, lat, lon, lat2,
                     lon2, counterpart, zone, severity, detail FROM events_v1;
            DROP TABLE events_v1;
        """)
        notes.append("events: added subkind to the uniqueness key "
                     "(re-run the detectors to recover findings dropped by v1)")

    if _table_exists(conn, "designations") and "basis" not in _columns(conn, "designations"):
        conn.execute("ALTER TABLE designations ADD COLUMN basis TEXT "
                     "NOT NULL DEFAULT 'direct'")
        notes.append("designations: added basis column (existing rows marked "
                     "'direct'; re-run sanctions sync to reclassify)")
    return notes


def connect(path: str = DEFAULT_DB, verbose: bool = True) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row

    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < SCHEMA_VERSION and _table_exists(conn, "positions"):
        for note in _migrate_v1(conn):
            if verbose:
                print(f"[migration] {note}")
        conn.commit()

    conn.executescript(SCHEMA)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------
def insert_positions(conn, rows) -> int:
    """Insert position rows, ignoring exact (mmsi, ts) duplicates.

    Returns the number actually stored. Callers should compare against the
    number offered: a large shortfall means upstream timestamps are colliding,
    which is a data problem worth surfacing rather than swallowing.
    """
    rows = list(rows)
    if not rows:
        return 0
    before = conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
    conn.executemany(
        """INSERT OR IGNORE INTO positions
           (mmsi, ts, lat, lon, sog, cog, heading, nav_status, draught, source)
           VALUES (:mmsi, :ts, :lat, :lon, :sog, :cog, :heading, :nav_status,
                   :draught, :source)""", rows)
    conn.commit()
    return conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0] - before


def upsert_vessel(conn, mmsi, ts, imo=None, name=None, callsign=None,
                  ship_type=None, flag=None, length_m=None, width_m=None):
    conn.execute(
        """INSERT INTO vessels (mmsi, imo, name, callsign, ship_type, flag,
                                length_m, width_m, first_seen, last_seen)
           VALUES (?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(mmsi, IFNULL(imo,''), IFNULL(name,'')) DO UPDATE SET
               first_seen = MIN(vessels.first_seen, excluded.first_seen),
               last_seen  = MAX(vessels.last_seen,  excluded.last_seen),
               callsign   = COALESCE(excluded.callsign, vessels.callsign),
               ship_type  = COALESCE(excluded.ship_type, vessels.ship_type),
               flag       = COALESCE(excluded.flag, vessels.flag),
               length_m   = COALESCE(excluded.length_m, vessels.length_m),
               width_m    = COALESCE(excluded.width_m, vessels.width_m)""",
        (str(mmsi), imo, name, callsign, ship_type, flag, length_m, width_m, ts, ts))


def insert_event(conn, kind, mmsi, start_ts, *, subkind="", imo=None, name=None,
                 end_ts=None, lat=None, lon=None, lat2=None, lon2=None,
                 counterpart="", zone=None, severity=0.0, detail=None) -> bool:
    cur = conn.execute(
        """INSERT OR IGNORE INTO events
           (kind, subkind, mmsi, imo, name, start_ts, end_ts, lat, lon, lat2,
            lon2, counterpart, zone, severity, detail)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (kind, subkind or "", str(mmsi), imo, name, start_ts, end_ts, lat, lon,
         lat2, lon2, counterpart or "", zone, severity, json.dumps(detail or {})))
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------
def watchlist_mmsis(conn, include_linked: bool = True):
    """MMSIs observed that map to a designated IMO."""
    sql = """SELECT DISTINCT v.mmsi FROM vessels v
             JOIN designations d ON d.imo = v.imo"""
    if not include_linked:
        sql += " WHERE d.basis <> 'linked'"
    return [r["mmsi"] for r in conn.execute(sql)]


def all_mmsis(conn, min_fixes: int = 2):
    return [r["mmsi"] for r in conn.execute(
        "SELECT mmsi FROM positions GROUP BY mmsi HAVING COUNT(*) >= ?",
        (min_fixes,))]


def track(conn, mmsi, since=None, until=None):
    sql, args = "SELECT * FROM positions WHERE mmsi = ?", [str(mmsi)]
    if since:
        sql += " AND ts >= ?"
        args.append(since)
    if until:
        sql += " AND ts <= ?"
        args.append(until)
    return conn.execute(sql + " ORDER BY ts", args).fetchall()


def identity(conn, mmsi):
    """Best-known (imo, name, flag) for an MMSI."""
    row = conn.execute(
        """SELECT imo, name, flag FROM vessels WHERE mmsi = ?
           ORDER BY (imo IS NULL), last_seen DESC LIMIT 1""", (str(mmsi),)).fetchone()
    return (row["imo"], row["name"], row["flag"]) if row else (None, None, None)


def is_designated(conn, imo) -> list:
    """Returns a list of (authority, program, basis) tuples."""
    if not imo:
        return []
    return [(r["authority"], r["program"], r["basis"]) for r in conn.execute(
        "SELECT authority, program, basis FROM designations WHERE imo = ?",
        (str(imo),))]
