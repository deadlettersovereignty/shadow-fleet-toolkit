#!/usr/bin/env python3
"""Generate a synthetic fleet so the pipeline can be exercised offline.

    shadowfleet testdata --db test.db

The fleet plants one instance of each detectable behaviour plus a control
vessel that must stay clean:

    ALPHA    goes dark for 31 h between Primorsk and the Danish Straits
    BRAVO    ship-to-ship rendezvous in the Laconian Gulf ...
    CHARLIE  ... the other half of it
    DELTA    one physically impossible position jump
    ECHO     tight circular track, the GNSS-spoofer artefact
    FOXTROT  uneventful transit - the control

Everything here is invented. MMSIs use real Maritime Identification Digits so
that the control vessel is genuinely clean (a made-up country prefix would
trip the identity checks and make the control useless), with obviously
synthetic suffixes. IMO numbers are synthetic but check-digit valid.
"""
from __future__ import annotations

import argparse
import math
from datetime import timedelta

from . import db
from .geo import iso, now_utc
from .ids import flag_from_mmsi

# Real MIDs: 538 Marshall Islands, 636 Liberia, 248 Malta, 341 St Kitts & Nevis.
FLEET = [
    ("538999801", 1, "TEST ALPHA",   "dark gap"),
    ("636999802", 2, "TEST BRAVO",   "sts partner A"),
    ("248999803", 3, "TEST CHARLIE", "sts partner B"),
    ("341999804", 4, "TEST DELTA",   "teleport"),
    ("538999805", 5, "TEST ECHO",    "circular track"),
    ("636999806", 6, "TEST FOXTROT", "control - must stay clean"),
]
ALIAS_MMSI = "248999899"                # ALPHA's IMO reappearing under a new MMSI


def make_imo(seed: int) -> str:
    """A check-digit-valid 7-digit number built from a 6-digit stem."""
    stem = f"{900000 + seed:06d}"
    return stem + str(sum(int(stem[i]) * (7 - i) for i in range(6)) % 10)


def step(lat, lon, bearing, nm):
    d = nm / 60.0
    return (lat + d * math.cos(math.radians(bearing)),
            lon + d * math.sin(math.radians(bearing))
            / max(0.1, math.cos(math.radians(lat))))


def emit(rows, mmsi, t, lat, lon, sog, nav=0):
    rows.append({"mmsi": mmsi, "ts": iso(t), "lat": round(lat, 5),
                 "lon": round(lon, 5), "sog": sog, "cog": None, "heading": None,
                 "nav_status": nav, "draught": None, "source": "synthetic"})


def build(conn):
    t0 = now_utc() - timedelta(days=20)
    rows = []
    for mmsi, seed, name, _ in FLEET:
        db.upsert_vessel(conn, mmsi, iso(t0), imo=make_imo(seed), name=name,
                         ship_type=80, flag=flag_from_mmsi(mmsi))

    alpha, bravo, charlie, delta, echo, foxtrot = (f[0] for f in FLEET)

    # ALPHA: departs Primorsk, goes dark, resurfaces near the Danish Straits.
    lat, lon, t = 60.34, 28.72, t0
    for i in range(40):
        emit(rows, alpha, t, lat, lon, 0.2 if i < 6 else 11.0)
        if i >= 6:
            lat, lon = step(lat, lon, 250, 11.0)
        t += timedelta(hours=1)
    t += timedelta(hours=31)                       # <- the dark period
    lat, lon = 57.72, 10.95
    for _ in range(30):
        emit(rows, alpha, t, lat, lon, 12.0)
        lat, lon = step(lat, lon, 240, 12.0)
        t += timedelta(hours=1)

    # BRAVO + CHARLIE: 8 h alongside in the Laconian Gulf.
    mlat, mlon = 36.55, 22.75
    t = t0 + timedelta(days=8)
    for i in range(90):                            # 15 h at 10 min spacing
        drift = i * 0.00015
        sog = 0.3 if 18 <= i <= 66 else 6.0
        emit(rows, bravo, t, mlat + drift, mlon + drift, sog)
        emit(rows, charlie, t, mlat + drift + 0.0018, mlon + drift + 0.0012, sog)
        t += timedelta(minutes=10)

    # DELTA: ordinary track with one impossible jump.
    lat, lon, t = 44.70, 37.79, t0 + timedelta(days=3)
    for i in range(60):
        emit(rows, delta, t, lat, lon, 10.0)
        lat, lon = step(lat, lon, 200, 10.0)
        t += timedelta(hours=1)
        if i == 30:
            lat, lon = lat + 3.5, lon + 5.0        # ~300 nm in one hour

    # ECHO: tight circle off Fujairah.
    clat, clon, t = 25.30, 56.55, t0 + timedelta(days=12)
    for i in range(64):
        lat, lon = step(clat, clon, (i * 22.5) % 360, 0.30)
        emit(rows, echo, t, lat, lon, 7.5)
        t += timedelta(minutes=20)

    # FOXTROT: uneventful transit, well clear of every zone.
    lat, lon, t = 51.0, 2.0, t0 + timedelta(days=1)
    for _ in range(120):
        emit(rows, foxtrot, t, lat, lon, 12.0)
        lat, lon = step(lat, lon, 300, 12.0)
        t += timedelta(hours=1)

    stored = db.insert_positions(conn, rows)

    # Identity laundering: ALPHA's IMO under a second MMSI and a new name.
    db.upsert_vessel(conn, ALIAS_MMSI, iso(t0 + timedelta(days=15)),
                     imo=make_imo(1), name="TEST ALPHA RENAMED", ship_type=80,
                     flag="Malta")

    conn.execute("""INSERT OR REPLACE INTO designations
                    (imo, name, authority, basis, program, listed_on, source,
                     fetched_at, raw)
                    VALUES (?,?,?,?,?,?,?,?,?)""",
                 (make_imo(1), "TEST ALPHA", "OFAC", "direct",
                  "RUSSIA-EO14024", None, "synthetic", iso(now_utc()), "{}"))
    conn.commit()
    return stored


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="shadowfleet testdata", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="test.db")
    args = ap.parse_args(argv)
    conn = db.connect(args.db)
    n = build(conn)
    conn.close()
    print(f"inserted {n} synthetic positions into {args.db}")
    print(f"now run: shadowfleet gaps --db {args.db} --min-hours 4, then "
          f"sts / spoof / risk / report")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
