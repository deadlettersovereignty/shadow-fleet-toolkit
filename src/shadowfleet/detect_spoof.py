#!/usr/bin/env python3
"""Detect AIS manipulation: position spoofing and identity laundering.

    shadowfleet spoof --since 2026-06-01
    shadowfleet spoof --checks teleport identity

Checks implemented:

  teleport   Consecutive fixes requiring an impossible speed. Classic GNSS
             spoofing, but also produced by two ships sharing one MMSI, so
             read it alongside the identity check.

  frozen     Position unchanged for hours while the vessel reports itself
             under way with headway. Either a stuck transponder or a static
             position being replayed.

  circle     Track traces a tight, near-perfect circle. The signature of
             several off-the-shelf GNSS spoofers, and not something a laden
             tanker does for hours at a time.

  identity   One IMO broadcast under several MMSIs, one MMSI broadcasting
             several IMOs, an MMSI whose country prefix is unassigned, or an
             IMO number that fails its own check digit. Flag-hopping and
             identity swaps are the least ambiguous shadow-fleet indicators
             available from AIS alone, so each distinct issue is recorded
             separately rather than collapsed into one finding per vessel.

Interpretation: any single hit can be a data error - AIS is a noisy,
unauthenticated VHF broadcast. Patterns repeating over weeks are the signal.
"""
from __future__ import annotations

import argparse
import math
from collections import defaultdict

from . import db
from .geo import angle_diff, bearing_deg, haversine_nm, parse_ts
from .ids import flag_from_mmsi, mmsi_looks_valid, valid_imo
from .zones import zone_for


def check_teleport(rows, max_kn, min_dist_nm):
    hits = []
    for prev, cur in zip(rows, rows[1:], strict=False):
        secs = (parse_ts(cur["ts"]) - parse_ts(prev["ts"])).total_seconds()
        if secs < 60 or secs > 6 * 3600:
            continue
        dist = haversine_nm(prev["lat"], prev["lon"], cur["lat"], cur["lon"])
        if dist < min_dist_nm:
            continue
        speed = dist / (secs / 3600.0)
        if speed > max_kn:
            hits.append({
                "subkind": "", "start_ts": prev["ts"], "end_ts": cur["ts"],
                "lat": prev["lat"], "lon": prev["lon"],
                "lat2": cur["lat"], "lon2": cur["lon"],
                "severity": min(100.0, 40 + speed / 2),
                "detail": {"implied_speed_kn": round(speed, 1),
                           "distance_nm": round(dist, 1), "interval_s": int(secs)}})
    return hits


def check_frozen(rows, min_hours, tol_deg=0.0005):
    hits, run = [], []
    for row in rows:
        if not run:
            run = [row]
            continue
        base = run[0]
        if (abs(row["lat"] - base["lat"]) < tol_deg
                and abs(row["lon"] - base["lon"]) < tol_deg):
            run.append(row)
        else:
            hits += _emit_frozen(run, min_hours)
            run = [row]
    return hits + _emit_frozen(run, min_hours)


def _emit_frozen(run, min_hours):
    if len(run) < 3:
        return []
    hours = (parse_ts(run[-1]["ts"]) - parse_ts(run[0]["ts"])).total_seconds() / 3600.0
    if hours < min_hours:
        return []
    moving = [r["sog"] for r in run if r["sog"] is not None and r["sog"] > 0.5]
    if len(moving) < max(3, len(run) // 4):
        return []                       # genuinely stopped; not interesting
    return [{"subkind": "", "start_ts": run[0]["ts"], "end_ts": run[-1]["ts"],
             "lat": run[0]["lat"], "lon": run[0]["lon"], "lat2": None, "lon2": None,
             "severity": min(100.0, 30 + hours * 2),
             "detail": {"hours": round(hours, 1), "fixes": len(run),
                        "reported_sog_max": max(moving),
                        "note": "position static while reporting headway"}}]


def check_circle(rows, window=16, cv_max=0.18, min_radius_nm=0.02,
                 max_radius_nm=6.0):
    hits, i = [], 0
    while i + window <= len(rows):
        win = rows[i:i + window]
        hours = (parse_ts(win[-1]["ts"]) - parse_ts(win[0]["ts"])).total_seconds() / 3600.0
        if hours <= 0 or hours > 12:
            i += 1
            continue
        clat = sum(r["lat"] for r in win) / window
        clon = sum(r["lon"] for r in win) / window
        radii = [haversine_nm(clat, clon, r["lat"], r["lon"]) for r in win]
        mean_r = sum(radii) / window
        if not (min_radius_nm <= mean_r <= max_radius_nm):
            i += 1
            continue
        var = sum((x - mean_r) ** 2 for x in radii) / window
        cv = math.sqrt(var) / mean_r if mean_r else 1.0
        if cv > cv_max:
            i += 1
            continue
        bearings = [bearing_deg(clat, clon, r["lat"], r["lon"]) for r in win]
        sweep = sum(abs(angle_diff(a, b)) for a, b in zip(bearings, bearings[1:], strict=False))
        if sweep < 300:
            i += 1
            continue
        hits.append({"subkind": "", "start_ts": win[0]["ts"], "end_ts": win[-1]["ts"],
                     "lat": clat, "lon": clon, "lat2": None, "lon2": None,
                     "severity": min(100.0, 55 + (cv_max - cv) * 200),
                     "detail": {"radius_nm": round(mean_r, 3),
                                "radius_cv": round(cv, 3),
                                "angular_sweep_deg": round(sweep),
                                "hours": round(hours, 2),
                                "note": "circular track - GNSS spoofer artefact"}})
        i += window                     # do not re-report the same loop
    return hits


def check_identity(conn):
    """Cross-vessel checks that need no position data.

    Each finding carries a distinct subkind so that several issues about the
    same vessel can coexist in the events table.
    """
    hits = []
    rows = conn.execute(
        "SELECT mmsi, imo, name, flag, first_seen, last_seen FROM vessels").fetchall()

    by_imo, by_mmsi, names = defaultdict(set), defaultdict(set), defaultdict(set)
    for r in rows:
        if r["imo"]:
            by_imo[r["imo"]].add(r["mmsi"])
            by_mmsi[r["mmsi"]].add(r["imo"])
        if r["name"]:
            names[r["mmsi"]].add(r["name"].strip().upper())

    for imo, mmsis in by_imo.items():
        if len(mmsis) > 1:
            flags = sorted({flag_from_mmsi(x) or "?" for x in mmsis})
            for m in sorted(mmsis):
                hits.append((m, imo, "multi_mmsi", 70 if len(flags) > 1 else 55, {
                    "issue": "one IMO broadcast under multiple MMSIs",
                    "mmsis": sorted(mmsis), "implied_flags": flags}))

    for mmsi, imos in by_mmsi.items():
        valid = {i for i in imos if valid_imo(i)}
        if len(valid) > 1:
            hits.append((mmsi, sorted(valid)[0], "multi_imo", 75, {
                "issue": "one MMSI broadcast with multiple IMO numbers",
                "imos": sorted(valid)}))
        for bad in sorted(imos - valid):
            hits.append((mmsi, None, "bad_imo_checksum", 45, {
                "issue": "IMO number fails check digit", "claimed_imo": bad}))

    for mmsi, seen in names.items():
        if len(seen) > 2:
            hits.append((mmsi, None, "name_churn", 50, {
                "issue": "vessel name changed repeatedly", "names": sorted(seen)}))

    for mmsi in {r["mmsi"] for r in rows}:
        if not mmsi_looks_valid(mmsi):
            hits.append((mmsi, None, "bad_mmsi_prefix", 40, {
                "issue": "MMSI is malformed or has no assigned country prefix"}))
    return hits


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="shadowfleet spoof", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=db.DEFAULT_DB)
    ap.add_argument("--checks", nargs="*",
                    choices=["teleport", "frozen", "circle", "identity"],
                    default=["teleport", "frozen", "circle", "identity"])
    ap.add_argument("--since")
    ap.add_argument("--until")
    ap.add_argument("--max-speed", type=float, default=28.0,
                    help="knots above which movement is implausible for a tanker")
    ap.add_argument("--min-teleport-nm", type=float, default=5.0)
    ap.add_argument("--frozen-hours", type=float, default=4.0)
    ap.add_argument("--watchlist-only", action="store_true")
    ap.add_argument("--limit", type=int, default=40)
    args = ap.parse_args(argv)

    conn = db.connect(args.db)
    results = []

    if {"teleport", "frozen", "circle"} & set(args.checks):
        mmsis = (db.watchlist_mmsis(conn) if args.watchlist_only
                 else db.all_mmsis(conn, 3))
        for mmsi in mmsis:
            rows = db.track(conn, mmsi, args.since, args.until)
            if len(rows) < 3:
                continue
            imo, name, _ = db.identity(conn, mmsi)
            plan = []
            if "teleport" in args.checks:
                plan.append(("teleport", check_teleport(
                    rows, args.max_speed, args.min_teleport_nm)))
            if "frozen" in args.checks:
                plan.append(("frozen", check_frozen(rows, args.frozen_hours)))
            if "circle" in args.checks:
                plan.append(("circle", check_circle(rows)))
            for kind, hits in plan:
                for h in hits:
                    db.insert_event(conn, kind, mmsi, h["start_ts"],
                                    subkind=h["subkind"], imo=imo, name=name,
                                    end_ts=h["end_ts"], lat=h["lat"], lon=h["lon"],
                                    lat2=h.get("lat2"), lon2=h.get("lon2"),
                                    zone=zone_for(h["lat"], h["lon"]),
                                    severity=h["severity"], detail=h["detail"])
                    results.append((h["severity"], kind, h["subkind"], mmsi,
                                    name, imo, h["start_ts"], h["detail"]))

    if "identity" in args.checks:
        for mmsi, imo, subkind, sev, detail in check_identity(conn):
            _, name, _ = db.identity(conn, mmsi)
            row = conn.execute(
                "SELECT MIN(first_seen) t FROM vessels WHERE mmsi=?", (mmsi,)).fetchone()
            ts = row["t"] or "1970-01-01T00:00:00Z"
            db.insert_event(conn, "identity", mmsi, ts, subkind=subkind, imo=imo,
                            name=name, severity=sev, detail=detail)
            results.append((sev, "identity", subkind, mmsi, name, imo, ts, detail))

    conn.commit()
    results.sort(reverse=True, key=lambda x: x[0])
    print(f"{len(results)} anomaly record(s)\n")
    for sev, kind, subkind, mmsi, name, imo, ts, detail in results[:args.limit]:
        label = f"{kind}/{subkind}" if subkind else kind
        print(f"[{sev:5.1f}] {label.upper():24s} {name or '?'}  "
              f"MMSI {mmsi}  IMO {imo or '?'}")
        print(f"          {ts}")
        for k, v in list(detail.items())[:4]:
            print(f"          {k}: {v}")
        print()
    if len(results) > args.limit:
        print(f"... {len(results) - args.limit} more")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
