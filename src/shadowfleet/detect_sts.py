#!/usr/bin/env python3
"""Detect probable ship-to-ship (STS) transfers.

    shadowfleet sts --since 2026-06-01
    shadowfleet sts --max-distance 0.4 --min-minutes 90 --zones-only

Definition used here: two vessels within --max-distance nautical miles of each
other, both making less than --max-speed knots, sustained for at least
--min-minutes.

Implementation notes:

* Comparing every pair of vessels at every timestamp is O(n^2) and does not
  finish on real data, so positions are snapped to time bins and bucketed into
  a spatial grid; only vessels in the same or adjacent cells are compared.
* The grid is sized from --max-distance and from the highest latitude present
  in the data, so one cell always spans at least the search radius in both
  axes. A fixed cell size silently misses pairs as soon as the radius is
  widened or the fleet moves north.
* Longitude cells wrap, so an encounter on the antimeridian is not split.
* Bins are processed as they stream out of SQLite rather than materialised, so
  memory is bounded by the busiest single bin instead of the whole query.

Caveats worth taking seriously:
  * tugs, bunker barges, pilot boats and vessels anchored on the same tide all
    reproduce this signature
  * a real STS is often half-visible - the receiving ship goes dark and only
    one side is in your data. Cross-reference the gap detector
  * closeness is not transfer. Confirm with draught changes, duration, and
    imagery where you can get it
"""
from __future__ import annotations

import argparse
import math
from collections import defaultdict
from datetime import timedelta

from . import db
from .geo import haversine_nm, iso, parse_ts
from .zones import category_of, zone_for


def build_grid(max_distance_nm: float, max_abs_lat: float):
    """Cell dimensions guaranteeing that a 3x3 neighbourhood covers the radius.

    Latitude is uniform (1 degree = 60 nm). Longitude degrees shrink with
    cos(lat), so the cell is widened using the worst-case latitude in the data.
    """
    cell_lat = max_distance_nm / 60.0
    cos_lat = max(math.cos(math.radians(min(abs(max_abs_lat), 89.0))), 1e-6)
    cell_lon = cell_lat / cos_lat
    n_lon = max(1, int(360.0 / cell_lon))   # floor keeps cells >= required width
    return cell_lat, 360.0 / n_lon, n_lon


def cell_of(lat, lon, cell_lat, cell_lon, n_lon):
    return (int(math.floor(lat / cell_lat)),
            int(math.floor((lon % 360.0) / cell_lon)) % n_lon)


def pairs_in_bin(cells, max_distance, n_lon):
    """Yield (mmsi_a, mmsi_b, distance_nm, midpoint) once per close pair."""
    for (ci, cj), members in cells.items():
        neighbours = {}
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                neighbours.update(cells.get((ci + di, (cj + dj) % n_lon), {}))
        for a, (lat_a, lon_a, _) in members.items():
            for b, (lat_b, lon_b, _) in neighbours.items():
                if a >= b:
                    continue
                d = haversine_nm(lat_a, lon_a, lat_b, lon_b)
                if d <= max_distance:
                    yield a, b, d, ((lat_a + lat_b) / 2, (lon_a + lon_b) / 2)


def _where(since, until):
    where, args = [], []
    if since:
        where.append("ts >= ?")
        args.append(since)
    if until:
        where.append("ts <= ?")
        args.append(until)
    return (" WHERE " + " AND ".join(where) if where else ""), args


def find_contacts(conn, since, until, bin_seconds, max_speed, max_distance):
    """Stream positions bin by bin, collecting per-pair contact records."""
    clause, args = _where(since, until)
    row = conn.execute(
        f"SELECT MAX(ABS(lat)) m FROM positions{clause}", args).fetchone()
    max_abs_lat = row["m"] if row and row["m"] is not None else 0.0
    cell_lat, cell_lon, n_lon = build_grid(max_distance, max_abs_lat)

    sql = f"SELECT mmsi, ts, lat, lon, sog FROM positions{clause} ORDER BY ts"
    contacts = defaultdict(list)
    epoch = None
    current_idx, current_cells = None, defaultdict(dict)
    total = 0

    def flush(idx, cells):
        for a, b, dist, mid in pairs_in_bin(cells, max_distance, n_lon):
            contacts[(a, b)].append((idx, dist, mid))

    for r in conn.execute(sql, args):
        sog = r["sog"]
        if sog is not None and sog > max_speed:
            continue                     # clearly under way; cannot be transferring
        ts = parse_ts(r["ts"])
        if epoch is None:
            epoch = ts
        idx = int((ts - epoch).total_seconds() // bin_seconds)
        if current_idx is None:
            current_idx = idx
        elif idx != current_idx:
            flush(current_idx, current_cells)
            current_cells, current_idx = defaultdict(dict), idx
        cell = cell_of(r["lat"], r["lon"], cell_lat, cell_lon, n_lon)
        current_cells[cell].setdefault(r["mmsi"], (r["lat"], r["lon"], sog))
        total += 1

    if current_idx is not None:
        flush(current_idx, current_cells)
    return contacts, epoch, total, (cell_lat, cell_lon, max_abs_lat)


def group_encounters(contacts, epoch, bin_seconds, min_minutes, zones_only):
    min_bins = max(1, int(min_minutes / (bin_seconds / 60.0)))
    events = []
    for pair, records in contacts.items():
        indices = [r[0] for r in records]
        runs, run = [], [indices[0]]
        for i in indices[1:]:
            if i - run[-1] <= 2:         # tolerate one missing bin
                run.append(i)
            else:
                runs.append(run)
                run = [i]
        runs.append(run)

        for r in runs:
            if len(r) < min_bins:
                continue
            span = [rec for rec in records if r[0] <= rec[0] <= r[-1]]
            dists = [rec[1] for rec in span]
            mid_lat = sum(rec[2][0] for rec in span) / len(span)
            mid_lon = sum(rec[2][1] for rec in span) / len(span)
            zone = zone_for(mid_lat, mid_lon)
            if zones_only and not zone:
                continue
            start = epoch + timedelta(seconds=r[0] * bin_seconds)
            end = epoch + timedelta(seconds=(r[-1] + 1) * bin_seconds)
            events.append({
                "a": pair[0], "b": pair[1], "start": start, "end": end,
                "minutes": (end - start).total_seconds() / 60.0,
                "lat": mid_lat, "lon": mid_lon, "zone": zone,
                "min_dist": min(dists), "mean_dist": sum(dists) / len(dists),
            })
    return events


def score(event, designated_either: bool) -> float:
    s = (25
         + min(35, event["minutes"] / 6.0)
         + (20 if event["zone"] else 0)
         + (15 if category_of(event["zone"]) == "sts" else 0)
         + (10 if event["min_dist"] < 0.15 else 0))
    if designated_either:
        s += 20
    return min(100.0, s)


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="shadowfleet sts", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=db.DEFAULT_DB)
    ap.add_argument("--max-distance", type=float, default=0.5, help="nautical miles")
    ap.add_argument("--max-speed", type=float, default=1.5, help="knots")
    ap.add_argument("--min-minutes", type=float, default=60.0)
    ap.add_argument("--bin-minutes", type=float, default=10.0)
    ap.add_argument("--since")
    ap.add_argument("--until")
    ap.add_argument("--zones-only", action="store_true",
                    help="only report rendezvous inside a known STS/terminal zone")
    ap.add_argument("--limit", type=int, default=40)
    args = ap.parse_args(argv)

    conn = db.connect(args.db)
    bin_seconds = args.bin_minutes * 60
    contacts, epoch, total, grid = find_contacts(
        conn, args.since, args.until, bin_seconds, args.max_speed, args.max_distance)
    if epoch is None:
        print("no slow-moving positions in range")
        return 0

    cell_lat, cell_lon, max_lat = grid
    print(f"scanned {total:,} slow fixes; grid {cell_lat*60:.2f} x "
          f"{cell_lon*60*math.cos(math.radians(min(max_lat,89))):.2f} nm "
          f"(worst-case latitude {max_lat:.1f})")

    events = group_encounters(contacts, epoch, bin_seconds,
                              args.min_minutes, args.zones_only)

    out = []
    for e in events:
        imo_a, name_a, _ = db.identity(conn, e["a"])
        imo_b, name_b, _ = db.identity(conn, e["b"])
        des_a = db.is_designated(conn, imo_a)
        des_b = db.is_designated(conn, imo_b)
        sev = score(e, bool(des_a or des_b))
        shared = {"duration_min": round(e["minutes"], 1),
                  "min_separation_nm": round(e["min_dist"], 3),
                  "mean_separation_nm": round(e["mean_dist"], 3)}
        # Written from both sides so each vessel carries the finding in its own
        # record; the mirror is marked so reports do not double-plot it.
        db.insert_event(conn, "sts", e["a"], iso(e["start"]), imo=imo_a,
                        name=name_a, end_ts=iso(e["end"]), lat=e["lat"],
                        lon=e["lon"], counterpart=e["b"], zone=e["zone"],
                        severity=sev,
                        detail={**shared, "counterpart_name": name_b,
                                "counterpart_imo": imo_b,
                                "designated_self": des_a,
                                "designated_counterpart": des_b})
        db.insert_event(conn, "sts", e["b"], iso(e["start"]), subkind="mirror",
                        imo=imo_b, name=name_b, end_ts=iso(e["end"]),
                        lat=e["lat"], lon=e["lon"], counterpart=e["a"],
                        zone=e["zone"], severity=sev,
                        detail={**shared, "counterpart_name": name_a,
                                "counterpart_imo": imo_a,
                                "designated_self": des_b,
                                "designated_counterpart": des_a})
        out.append((sev, e, name_a, name_b, des_a, des_b))

    conn.commit()
    out.sort(reverse=True, key=lambda x: x[0])
    print(f"\n{len(out)} probable rendezvous\n")
    for sev, e, na, nb, da, db_ in out[:args.limit]:
        mark = " *" if (da or db_) else ""
        print(f"[{sev:5.1f}] {na or '?'} ({e['a']})  <->  {nb or '?'} ({e['b']}){mark}")
        print(f"         {e['start']:%Y-%m-%d %H:%M}Z for {e['minutes']:.0f} min, "
              f"closest {e['min_dist']*1852:.0f} m")
        print(f"         {e['lat']:.4f}, {e['lon']:.4f}"
              f"{'  in ' + e['zone'] if e['zone'] else ''}\n")
    if any(d or e for _, _, _, _, d, e in out):
        print("* = at least one party appears on a sanctions list")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
