#!/usr/bin/env python3
"""Find AIS transmission gaps ("going dark").

    shadowfleet gaps --min-hours 6
    shadowfleet gaps --watchlist-only --min-hours 4 --since 2026-06-01

A gap is the interval between two consecutive fixes for one MMSI that exceeds
--min-hours.

The hard part is that most gaps are boring. A vessel mid-ocean with no
satellite pass overhead looks identical to one that pulled the breaker on its
transponder. This script therefore scores gaps rather than just listing them:

  * gaps that BEGIN inside a well-covered coastal zone are more meaningful
    than gaps that begin in open ocean
  * gaps that end far from where they began, at an implied speed the vessel
    could plausibly have sustained, suggest a real voyage was concealed
  * gaps that begin and end in nearly the same place suggest either a receiver
    outage or a stationary vessel that simply stopped reporting
  * gaps bracketing a known STS anchorage or Russian export terminal are the
    interesting ones

A high score is a reason to go look at satellite imagery or port records. It
is not evidence on its own.
"""
from __future__ import annotations

import argparse

from . import db
from .geo import haversine_nm, parse_ts
from .zones import category_of, zone_for


def score_gap(hours, distance_nm, implied_kn, start_zone, end_zone):
    """0-100 triage score. Weights are judgement calls - tune them."""
    pts, why = 0.0, []

    if hours >= 72:
        pts += 30
        why.append("gap >72h")
    elif hours >= 24:
        pts += 22
        why.append("gap >24h")
    elif hours >= 12:
        pts += 14
        why.append("gap >12h")
    else:
        pts += 7
        why.append("gap >min threshold")

    if start_zone:
        pts += 20
        why.append(f"went dark in {start_zone} (good AIS coverage expected)")
    if end_zone:
        pts += 12
        why.append(f"reappeared in {end_zone}")

    for z in (start_zone, end_zone):
        cat = category_of(z) if z else None
        if cat == "ru_terminal":
            pts += 18
            why.append("adjacent to Russian export terminal")
        elif cat == "sts":
            pts += 15
            why.append("adjacent to known STS anchorage")

    if distance_nm < 2:
        pts -= 10
        why.append("reappeared at same position (likely receiver outage)")
    elif 3 <= implied_kn <= 16:
        pts += 12
        why.append(f"moved {distance_nm:.0f} nm at a plausible {implied_kn:.1f} kn")
    elif implied_kn > 25:
        pts += 5
        why.append(f"implied {implied_kn:.1f} kn - check for identity confusion")

    return max(0.0, min(100.0, pts)), why


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="shadowfleet gaps", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=db.DEFAULT_DB)
    ap.add_argument("--min-hours", type=float, default=6.0)
    ap.add_argument("--max-hours", type=float, default=24 * 60,
                    help="ignore gaps longer than this (usually means the "
                         "vessel left the collection area entirely)")
    ap.add_argument("--since", help="ISO date, e.g. 2026-06-01")
    ap.add_argument("--until")
    ap.add_argument("--watchlist-only", action="store_true")
    ap.add_argument("--min-score", type=float, default=0.0)
    ap.add_argument("--limit", type=int, default=40)
    args = ap.parse_args(argv)

    conn = db.connect(args.db)
    mmsis = db.watchlist_mmsis(conn) if args.watchlist_only else db.all_mmsis(conn)
    if not mmsis:
        print("no vessels to analyse (empty database, or empty watchlist)")
        return 0

    found = []
    for mmsi in mmsis:
        rows = db.track(conn, mmsi, args.since, args.until)
        if len(rows) < 2:
            continue
        imo, name, _flag = db.identity(conn, mmsi)
        prev = rows[0]
        for cur in rows[1:]:
            t0, t1 = parse_ts(prev["ts"]), parse_ts(cur["ts"])
            hours = (t1 - t0).total_seconds() / 3600.0
            if not (args.min_hours <= hours <= args.max_hours):
                prev = cur
                continue

            dist = haversine_nm(prev["lat"], prev["lon"], cur["lat"], cur["lon"])
            implied = dist / hours if hours else 0.0
            z0 = zone_for(prev["lat"], prev["lon"])
            z1 = zone_for(cur["lat"], cur["lon"])
            score, why = score_gap(hours, dist, implied, z0, z1)

            if score >= args.min_score:
                detail = {"hours": round(hours, 2), "distance_nm": round(dist, 1),
                          "implied_speed_kn": round(implied, 2),
                          "start_zone": z0, "end_zone": z1,
                          "last_sog": prev["sog"], "reasons": why}
                db.insert_event(conn, "gap", mmsi, prev["ts"], imo=imo, name=name,
                                end_ts=cur["ts"], lat=prev["lat"], lon=prev["lon"],
                                lat2=cur["lat"], lon2=cur["lon"],
                                zone=z0 or z1, severity=score, detail=detail)
                found.append((score, mmsi, name or "?", imo or "?", prev["ts"],
                              hours, dist, z0, z1, why))
            prev = cur

    conn.commit()
    found.sort(reverse=True, key=lambda x: x[0])
    print(f"{len(found)} gap(s) >= {args.min_hours}h across {len(mmsis)} vessel(s)\n")
    for (score, mmsi, name, imo, start, hours, dist, z0, z1, why) in found[:args.limit]:
        head = f"[{score:5.1f}] {name}  MMSI {mmsi}  IMO {imo}"
        print(head)
        print(f"         dark {hours:.1f}h from {start}, resurfaced {dist:.0f} nm away")
        if z0 or z1:
            print(f"         {z0 or 'open sea'}  ->  {z1 or 'open sea'}")
        print(f"         {'; '.join(why[:3])}\n")
    if len(found) > args.limit:
        print(f"... {len(found) - args.limit} more (raise --limit or --min-score)")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
