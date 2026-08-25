#!/usr/bin/env python3
"""Combine every signal into one ranked risk score per vessel.

    shadowfleet risk --top 25
    shadowfleet risk --export risk.csv --enrich fleet_metadata.csv

--enrich accepts a CSV with an `imo` column plus any of: built_year, dwt,
flag, owner, manager, pi_club, class_society. Those fields cannot be obtained
from AIS and have to come from a registry (IHS/Equasis/Lloyd's) or your own
research; the scorer uses them when present and skips those factors when not.

The weights below are a defensible starting point, not a standard. Publish
them alongside any findings so a reader can disagree with your arithmetic
rather than just with your conclusion.
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from . import db
from .geo import iso, now_utc
from .ids import is_elevated_risk_flag
from .zones import category_of, zone_for

WEIGHTS = {
    "designated_ofac":   35,
    "designated_eu":     30,
    "designated_uk":     30,
    "designated_other":  15,
    # A vessel merely named in another entity's remarks is a lead, not a
    # listing, so it must not score like one.
    "designated_linked_factor":    0.35,
    "designated_unverified_factor": 0.7,
    "gap_each":           6,   # capped
    "gap_cap":           24,
    "gap_at_terminal":   12,
    "sts_each":           8,
    "sts_cap":           24,
    "sts_in_hotspot":    10,
    "spoof_teleport":    18,
    "spoof_circle":      22,
    "spoof_frozen":      10,
    "identity_swap":     25,
    "elevated_flag":     10,
    "unknown_flag":      12,
    "russian_terminal_call": 15,
    "age_over_15":        6,
    "age_over_20":       12,
    "no_imo":            10,
}

BANDS = [(75, "critical"), (50, "high"), (30, "medium"), (0, "low")]


def band(score):
    for cut, label in BANDS:
        if score >= cut:
            return label
    return "low"


def load_enrichment(path):
    if not path:
        return {}
    out = {}
    with open(path, encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            key = (row.get("imo") or row.get("IMO") or "").strip()
            if key:
                out[key] = {k.lower(): v for k, v in row.items() if v}
    return out


def terminal_calls(conn, mmsi, since_iso):
    """Stops (<1 kn) inside a Russian export terminal zone."""
    rows = conn.execute(
        "SELECT lat, lon, sog FROM positions WHERE mmsi=? AND ts>=? AND "
        "(sog IS NULL OR sog < 1.0)", (mmsi, since_iso)).fetchall()
    hits = set()
    for r in rows:
        z = zone_for(r["lat"], r["lon"], categories={"ru_terminal"})
        if z:
            hits.add(z)
    return sorted(hits)


def score_vessel(conn, mmsi, events, enrich, since_iso):
    imo, name, flag = db.identity(conn, mmsi)
    factors = []

    def add(code, points, note):
        if points:
            factors.append({"code": code, "points": round(points, 1), "note": note})

    # --- sanctions ---------------------------------------------------------
    for authority, program, basis in db.is_designated(conn, imo):
        key = f"designated_{authority.lower()}"
        points = WEIGHTS.get(key, WEIGHTS["designated_other"])
        note = f"designated by {authority}" + (f" ({program})" if program else "")
        if basis == "linked":
            points *= WEIGHTS["designated_linked_factor"]
            note = (f"named in a designated entity's remarks ({authority})"
                    " - confirm before treating as a listing")
        elif basis == "unverified":
            points *= WEIGHTS["designated_unverified_factor"]
            note += " (attribution unverified)"
        add(key, points, note)

    # --- behavioural -------------------------------------------------------
    gaps = [e for e in events if e["kind"] == "gap"]
    if gaps:
        add("gaps", min(WEIGHTS["gap_cap"], WEIGHTS["gap_each"] * len(gaps)),
            f"{len(gaps)} AIS gap(s), longest "
            f"{max(json.loads(e['detail']).get('hours', 0) for e in gaps):.0f}h")
        if any(category_of(e["zone"]) == "ru_terminal" for e in gaps if e["zone"]):
            add("gap_at_terminal", WEIGHTS["gap_at_terminal"],
                "went dark at or near a Russian export terminal")

    sts = [e for e in events if e["kind"] == "sts"]
    if sts:
        add("sts", min(WEIGHTS["sts_cap"], WEIGHTS["sts_each"] * len(sts)),
            f"{len(sts)} probable ship-to-ship rendezvous")
        if any(category_of(e["zone"]) == "sts" for e in sts if e["zone"]):
            add("sts_in_hotspot", WEIGHTS["sts_in_hotspot"],
                "rendezvous inside a known STS anchorage")

    for kind, key in (("teleport", "spoof_teleport"), ("circle", "spoof_circle"),
                      ("frozen", "spoof_frozen")):
        n = sum(1 for e in events if e["kind"] == kind)
        if n:
            add(key, WEIGHTS[key], f"{n} {kind} anomal{'y' if n == 1 else 'ies'}")

    ident = [e for e in events if e["kind"] == "identity"]
    if ident:
        issues = {json.loads(e["detail"]).get("issue") for e in ident}
        add("identity", WEIGHTS["identity_swap"], "; ".join(sorted(filter(None, issues))))

    # --- registry / static -------------------------------------------------
    if not imo:
        add("no_imo", WEIGHTS["no_imo"], "no IMO number ever broadcast")

    meta = enrich.get(imo or "", {})
    flag = meta.get("flag") or flag
    if not flag:
        add("unknown_flag", WEIGHTS["unknown_flag"], "flag state undetermined")
    elif is_elevated_risk_flag(flag):
        add("elevated_flag", WEIGHTS["elevated_flag"],
            f"{flag} registry - frequently used by opaque tanker tonnage")

    built = meta.get("built_year") or meta.get("built")
    if built:
        try:
            age = datetime.now(timezone.utc).year - int(str(built)[:4])
            if age >= 20:
                add("age_over_20", WEIGHTS["age_over_20"], f"{age} years old")
            elif age >= 15:
                add("age_over_15", WEIGHTS["age_over_15"], f"{age} years old")
        except ValueError:
            pass

    calls = terminal_calls(conn, mmsi, since_iso)
    if calls:
        add("russian_terminal_call", WEIGHTS["russian_terminal_call"],
            "stopped at " + ", ".join(calls))

    total = min(100.0, sum(f["points"] for f in factors))
    return {"mmsi": mmsi, "imo": imo, "name": name, "flag": flag,
            "score": round(total, 1), "band": band(total), "factors": factors}


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="shadowfleet risk", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=db.DEFAULT_DB)
    ap.add_argument("--days", type=int, default=180,
                    help="look-back window for behavioural signals")
    ap.add_argument("--enrich", help="CSV of registry metadata keyed on imo")
    ap.add_argument("--export", help="write full results to this CSV")
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--min-score", type=float, default=1.0)
    args = ap.parse_args(argv)

    conn = db.connect(args.db)
    since_iso = iso(now_utc() - timedelta(days=args.days))
    enrich = load_enrichment(args.enrich)

    by_mmsi = defaultdict(list)
    for e in conn.execute("SELECT * FROM events WHERE start_ts >= ?", (since_iso,)):
        by_mmsi[e["mmsi"]].append(e)

    candidates = set(by_mmsi) | set(db.watchlist_mmsis(conn))
    if not candidates:
        print("nothing to score - run the collectors and detectors first")
        return 0

    scored = []
    scored_at = iso(now_utc())
    for mmsi in candidates:
        rec = score_vessel(conn, mmsi, by_mmsi.get(mmsi, []), enrich, since_iso)
        if rec["score"] < args.min_score:
            continue
        conn.execute(
            """INSERT INTO risk (mmsi, imo, name, flag, score, band, factors, scored_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(mmsi) DO UPDATE SET
                 imo=excluded.imo, name=excluded.name, flag=excluded.flag,
                 score=excluded.score, band=excluded.band,
                 factors=excluded.factors, scored_at=excluded.scored_at""",
            (rec["mmsi"], rec["imo"], rec["name"], rec["flag"], rec["score"],
             rec["band"], json.dumps(rec["factors"]), scored_at))
        scored.append(rec)
    conn.commit()

    scored.sort(key=lambda r: -r["score"])
    print(f"scored {len(scored)} vessel(s) over the last {args.days} days\n")
    print(f"{'SCORE':>6} {'BAND':<9} {'NAME':<24} {'MMSI':<11} {'IMO':<9} FLAG")
    print("-" * 88)
    for r in scored[:args.top]:
        print(f"{r['score']:6.1f} {r['band']:<9} {(r['name'] or '?')[:23]:<24} "
              f"{r['mmsi']:<11} {(r['imo'] or '?'):<9} {r['flag'] or '?'}")
        for f in r["factors"][:3]:
            print(f"       +{f['points']:<5.1f} {f['note']}")
    if args.export:
        with open(args.export, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["score", "band", "name", "mmsi", "imo", "flag", "factors"])
            for r in scored:
                w.writerow([r["score"], r["band"], r["name"] or "", r["mmsi"],
                            r["imo"] or "", r["flag"] or "",
                            "; ".join(f"{f['code']}(+{f['points']})" for f in r["factors"])])
        print(f"\nwrote {args.export}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
