#!/usr/bin/env python3
"""Load archived AIS from CSV or raw NMEA into SQLite.

    shadowfleet ingest aisdk-2024-05-01.csv          # Danish Maritime Authority
    shadowfleet ingest feed.nmea --format nmea       # needs the [nmea] extra
    shadowfleet ingest custom.csv --map ts=Timestamp,mmsi=MMSI,lat=Lat,lon=Lon

Useful free archives:
  * Danish Maritime Authority - https://web.ais.dk/aisdata/  (Baltic and the
    Danish Straits, daily CSV, no registration)
  * NOAA Marine Cadastre - https://marinecadastre.gov/ais/  (US waters)
  * BarentsWatch / Norwegian Coastal Administration (registration required)

Timestamps in NMEA
------------------
Raw AIS position sentences carry no date - only a second-of-minute field. A
timestamp therefore has to come from the capture, not the payload:

  * TAG blocks (``\\c:1671620143\\!AIVDM,...``) carry a receiver epoch and are
    used automatically when present
  * a leading epoch or ISO timestamp column ahead of the ``!AIVDM`` is parsed
  * ``--live`` stamps sentences with the current clock, which is correct only
    when reading a live feed

If none of those apply the import aborts. Stamping an archive with the import
time silently collapses every voyage to a single point, because ``(mmsi, ts)``
is unique.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import io
import re
import sys
import zipfile
from pathlib import Path

from . import db
from .geo import iso, now_utc, parse_ts
from .ids import flag_from_mmsi, valid_imo

ALIASES = {
    "ts":         ["# timestamp", "timestamp", "basedatetime", "time", "datetime",
                   "date_time_utc"],
    "mmsi":       ["mmsi", "userid"],
    "lat":        ["latitude", "lat", "y"],
    "lon":        ["longitude", "lon", "long", "lng", "x"],
    "sog":        ["sog", "speed", "speedoverground"],
    "cog":        ["cog", "course", "courseoverground"],
    "heading":    ["heading", "trueheading", "hdg"],
    "nav_status": ["navigational status", "navstatus", "status"],
    "draught":    ["draught", "draft"],
    "imo":        ["imo", "imonumber"],
    "name":       ["name", "vesselname", "shipname"],
    "callsign":   ["callsign", "call sign"],
    "ship_type":  ["ship type", "shiptype", "vesseltype", "type"],
}

NAV_STATUS_TEXT = {
    "under way using engine": 0, "at anchor": 1, "not under command": 2,
    "restricted maneuverability": 3, "constrained by her draught": 4,
    "moored": 5, "aground": 6, "engaged in fishing": 7, "under way sailing": 8,
}

_LEADING_TS = re.compile(r"^\s*([0-9]{9,13}(?:\.\d+)?|[0-9T:\-\.Z+]{19,32})\s*[,;\t ]\s*(?=[\\!$])")


def opener(path: str):
    p = Path(path)
    if p.suffix == ".gz":
        return gzip.open(p, "rt", encoding="utf-8", errors="replace")
    if p.suffix == ".zip":
        zf = zipfile.ZipFile(p)
        return io.TextIOWrapper(zf.open(zf.namelist()[0]),
                                encoding="utf-8", errors="replace")
    return open(p, encoding="utf-8", errors="replace")


def build_mapping(fieldnames, overrides):
    norm = {f.lower().strip(): f for f in fieldnames}
    mapping = {}
    for key, names in ALIASES.items():
        for cand in names:
            if cand in norm:
                mapping[key] = norm[cand]
                break
    mapping.update(overrides)
    missing = [k for k in ("ts", "mmsi", "lat", "lon") if k not in mapping]
    if missing:
        sys.exit(f"could not find column(s) for {missing}.\n"
                 f"available: {list(fieldnames)}\n"
                 f"use --map ts=...,mmsi=...,lat=...,lon=...")
    return mapping


def num(row, mapping, key):
    col = mapping.get(key)
    if not col:
        return None
    raw = (row.get(col) or "").strip()
    if not raw or raw.lower() in {"unknown", "na", "n/a", "null", "undefined"}:
        return None
    try:
        return float(raw.replace(",", "."))
    except ValueError:
        return NAV_STATUS_TEXT.get(raw.lower()) if key == "nav_status" else None


def _is_tanker(row, mapping, stype_num):
    """AIS ship type 80-89 is 'tanker'. DMA writes it as text instead."""
    if stype_num and 80 <= int(stype_num) <= 89:
        return True
    col = mapping.get("ship_type")
    return "tanker" in (row.get(col) or "").lower() if col else False


def _report(label, offered, stored, skipped, vessels=None):
    dropped = offered - stored - skipped
    line = (f"  rows={offered:,} stored={stored:,} unparseable={skipped:,} "
            f"duplicate={dropped:,}")
    if vessels is not None:
        line += f" identities={vessels:,}"
    print(line)
    if offered and dropped > offered * 0.2:
        print(f"  ! {dropped/offered:.0%} of {label} collided on (mmsi, ts). "
              f"Check that source timestamps are real and distinct.")


def import_csv(path, conn, overrides, source, batch, tankers_only, dayfirst):
    offered = stored = skipped = 0
    with opener(path) as fh:
        reader = csv.DictReader(fh)
        mapping = build_mapping(reader.fieldnames or [], overrides)
        print(f"  columns: {mapping}")
        buf, statics = [], {}
        for row in reader:
            offered += 1
            try:
                mmsi = str(int(float(row[mapping["mmsi"]])))
                lat = float(row[mapping["lat"]])
                lon = float(row[mapping["lon"]])
                ts = iso(parse_ts(row[mapping["ts"]], dayfirst=dayfirst))
            except (ValueError, KeyError, TypeError):
                skipped += 1
                continue
            if abs(lat) > 90 or abs(lon) > 180:
                skipped += 1
                continue

            stype = num(row, mapping, "ship_type")
            if tankers_only and not _is_tanker(row, mapping, stype):
                skipped += 1
                continue

            buf.append({"mmsi": mmsi, "ts": ts, "lat": lat, "lon": lon,
                        "sog": num(row, mapping, "sog"),
                        "cog": num(row, mapping, "cog"),
                        "heading": num(row, mapping, "heading"),
                        "nav_status": num(row, mapping, "nav_status"),
                        "draught": num(row, mapping, "draught"),
                        "source": source})

            imo_raw = (row.get(mapping.get("imo", ""), "") or "").strip()
            name = (row.get(mapping.get("name", ""), "") or "").strip() or None
            imo = imo_raw if valid_imo(imo_raw) else None
            # Keyed on the identity tuple, not just the MMSI: a vessel that
            # changes name or IMO mid-file is exactly what we are looking for.
            key = (mmsi, imo, name)
            if (name or imo) and key not in statics:
                statics[key] = (ts, (row.get(mapping.get("callsign", ""), "")
                                     or "").strip() or None,
                                int(stype) if stype else None)

            if len(buf) >= batch:
                stored += db.insert_positions(conn, buf)
                buf.clear()
                print(f"\r  rows={offered:,} stored={stored:,}", end="")
        if buf:
            stored += db.insert_positions(conn, buf)

    print("\r", end="")
    for (mmsi, imo, name), (ts, cs, stype) in statics.items():
        db.upsert_vessel(conn, mmsi, ts, imo=imo, name=name, callsign=cs,
                         ship_type=stype, flag=flag_from_mmsi(mmsi))
    conn.commit()
    _report("rows", offered, stored, skipped, len(statics))


def _nmea_timestamp(raw_line, msg, live):
    """Resolve an observation time, or None if the sentence carries none."""
    tb = getattr(msg, "tag_block", None)
    if tb is not None:
        try:
            tb.init()
            epoch = getattr(tb, "receiver_timestamp", None)
            if epoch:
                return parse_ts(float(epoch))
        except Exception:               # noqa: BLE001 - malformed tag block
            pass
    m = _LEADING_TS.match(raw_line)
    if m:
        token = m.group(1)
        try:
            return parse_ts(float(token) if token.replace(".", "").isdigit()
                            else token)
        except ValueError:
            pass
    return now_utc() if live else None


def import_nmea(path, conn, source, batch, live):
    try:
        from pyais.stream import FileReaderStream
    except ImportError:
        sys.exit("NMEA support needs pyais: pip install 'shadow-fleet-toolkit[nmea]'")

    buf, offered, stored, skipped, undated = [], 0, 0, 0, 0
    raw_lines = [ln.rstrip("\n") for ln in opener(path)]
    for i, msg in enumerate(FileReaderStream(str(path))):
        offered += 1
        try:
            d = msg.decode().asdict()
        except Exception:               # noqa: BLE001
            skipped += 1
            continue
        when = _nmea_timestamp(raw_lines[i] if i < len(raw_lines) else "", msg, live)
        if when is None:
            undated += 1
            if undated > 25:            # fail fast on a whole undated file
                sys.exit(
                    f"\n{path}: sentences carry no timestamp (no TAG block, no "
                    f"leading time column).\nRe-run with --live only if this is "
                    f"a live capture; otherwise these fixes cannot be placed in "
                    f"time and importing them would corrupt the database.")
            continue
        ts = iso(when)
        mmsi = str(d.get("mmsi") or "")
        if d.get("msg_type") in (1, 2, 3, 18, 19) and d.get("lat") is not None:
            if abs(d["lat"]) > 90 or abs(d["lon"]) > 180:
                skipped += 1
                continue
            buf.append({"mmsi": mmsi, "ts": ts, "lat": d["lat"], "lon": d["lon"],
                        "sog": d.get("speed"), "cog": d.get("course"),
                        "heading": d.get("heading"), "nav_status": d.get("status"),
                        "draught": None, "source": source})
        elif d.get("msg_type") == 5:
            imo = d.get("imo")
            db.upsert_vessel(conn, mmsi, ts,
                             imo=str(imo) if valid_imo(imo) else None,
                             name=(d.get("shipname") or "").strip() or None,
                             callsign=(d.get("callsign") or "").strip() or None,
                             ship_type=d.get("ship_type"),
                             flag=flag_from_mmsi(mmsi))
        if len(buf) >= batch:
            stored += db.insert_positions(conn, buf)
            buf.clear()
    if buf:
        stored += db.insert_positions(conn, buf)
    conn.commit()
    _report("sentences", offered, stored, skipped + undated)


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="shadowfleet ingest", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+")
    ap.add_argument("--db", default=db.DEFAULT_DB)
    ap.add_argument("--format", choices=["csv", "nmea"], default="csv")
    ap.add_argument("--map", default="", help="column overrides, e.g. ts=Time,mmsi=MMSI")
    ap.add_argument("--batch", type=int, default=20000)
    ap.add_argument("--tankers-only", action="store_true",
                    help="keep only AIS ship types 80-89")
    ap.add_argument("--month-first", action="store_true",
                    help="read ambiguous dd/mm/yyyy dates as mm/dd/yyyy")
    ap.add_argument("--live", action="store_true",
                    help="NMEA only: stamp undated sentences with the current "
                         "clock (correct for a live capture, wrong for archives)")
    args = ap.parse_args(argv)

    overrides = dict(kv.split("=", 1) for kv in args.map.split(",") if "=" in kv)
    conn = db.connect(args.db)
    for path in args.files:
        print(f"[{path}]")
        if args.format == "nmea":
            import_nmea(path, conn, Path(path).name, args.batch, args.live)
        else:
            import_csv(path, conn, overrides, Path(path).name, args.batch,
                       args.tankers_only, not args.month_first)
    n = conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
    print(f"\ntotal positions in database: {n:,}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
