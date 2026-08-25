#!/usr/bin/env python3
"""Build a master list of designated vessels (by IMO) from official sources.

    shadowfleet sanctions --source ofac
    shadowfleet sanctions --source all --export watchlist.csv
    shadowfleet sanctions --file downloads/uk_list.csv --authority UK

Download URLs move around. Every source can be pointed at a file you
downloaded yourself with --file, which is the more reliable workflow on a
schedule.

Attribution matters here more than anywhere else in the toolkit. A record is
stored with one of three bases:

  direct      the list entry is the vessel itself; its name is the vessel name
  linked      the IMO was mentioned in some other entity's remarks - typically
              an owner or manager designated together with tonnage. The vessel
              may or may not be designated in its own right, and the entry's
              name belongs to the company, not the ship, so no name is stored
  unverified  extracted by the format-agnostic parser, where the association
              between a name and an IMO in the same record is a guess

Only 'direct' is ingested unless --include-linked is passed. Treating a
mention as a designation puts undesignated hulls on a sanctions list, which is
the most consequential error this tool can make.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys

from . import db
from .geo import iso, now_utc
from .ids import extract_imos, valid_imo

try:
    import requests
except ImportError:                     # pragma: no cover
    requests = None

SOURCES = {
    "ofac": [
        "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN.CSV",
        "https://www.treasury.gov/ofac/downloads/sdn.csv",
    ],
    "eu": [
        "https://webgate.ec.europa.eu/fsd/fsf/public/files/csvFullSanctionsList_1_1/content?token=dG9rZW4tMjAxNw",
        "https://webgate.ec.europa.eu/fsd/fsf/public/files/xmlFullSanctionsList_1_1/content?token=dG9rZW4tMjAxNw",
    ],
    "uk": [
        "https://assets.publishing.service.gov.uk/media/consolidated_list.csv",
    ],
}

# OFAC SDN.CSV is headerless with these 12 columns.
OFAC_COLS = ["ent_num", "name", "sdn_type", "program", "title", "callsign",
             "vessel_type", "tonnage", "grt", "flag", "owner", "remarks"]

UA = {"User-Agent": "shadow-fleet-toolkit (research)"}
BASIS_RANK = {"linked": 0, "unverified": 1, "direct": 2}


def fetch(urls, timeout=60):
    if requests is None:
        sys.exit("pip install 'shadow-fleet-toolkit[all]', or use --file")
    last = None
    for url in urls:
        try:
            r = requests.get(url, headers=UA, timeout=timeout)
            r.raise_for_status()
            print(f"  fetched {len(r.content):,} bytes from {url[:70]}...")
            return r.text, url
        except Exception as exc:        # noqa: BLE001
            print(f"  ! {url[:60]}... -> {exc}")
            last = exc
    raise RuntimeError(f"all URLs failed: {last}")


# ---------------------------------------------------------------------------
# Parsers -> list of {imo, name, program, listed_on, basis, raw}
# ---------------------------------------------------------------------------
def parse_ofac(text: str):
    out = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 12:
            continue
        rec = dict(zip(OFAC_COLS, (c.strip().strip('"') for c in row[:12]), strict=False))
        is_vessel = rec["sdn_type"].lower() == "vessel"
        imos = extract_imos(rec["remarks"])
        if is_vessel and not imos:
            imos = extract_imos(rec["name"])
        raw = json.dumps({k: v for k, v in rec.items() if v and v != "-0-"})
        for imo in imos:
            out.append({
                "imo": imo,
                # A company's name is not its fleet's name.
                "name": _clean(rec["name"]) if is_vessel else None,
                "program": rec["program"] or None,
                "listed_on": None,
                "basis": "direct" if is_vessel else "linked",
                "via": None if is_vessel else _clean(rec["name"]),
                "raw": raw,
            })
    return out


NAME_HINT = re.compile(r'(?:name["\'>\s:=]{1,6})([A-Z][A-Z0-9 .\'\-]{2,40})',
                       re.IGNORECASE)


def parse_generic(text: str):
    """Format-agnostic fallback for EU / UK / anything else."""
    chunks = re.split(r"(?:\n\s*\n)|(?=<sanctionEntity)|(?=<ENTITY)", text)
    out, seen = [], set()
    for chunk in chunks:
        imos = extract_imos(chunk)
        if not imos:
            continue
        m = NAME_HINT.search(chunk)
        pm = re.search(r"(Russia|Ukraine|Iran|DPRK|Venezuela|Syria)[\w /-]{0,30}",
                       chunk, re.IGNORECASE)
        for imo in imos:
            if imo in seen:
                continue
            seen.add(imo)
            out.append({"imo": imo, "name": _clean(m.group(1)) if m else None,
                        "program": pm.group(0).strip() if pm else None,
                        "listed_on": None, "basis": "unverified", "via": None,
                        "raw": chunk[:800]})
    return out


def parse_csv_file(text: str):
    """User-supplied CSV carrying an imo column and optionally a name."""
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return parse_generic(text)
    lower = {f.lower().strip(): f for f in reader.fieldnames}
    imo_col = next((lower[k] for k in lower if "imo" in k), None)
    if not imo_col:
        return parse_generic(text)
    name_col = next((lower[k] for k in lower if "name" in k or "vessel" in k), None)
    prog_col = next((lower[k] for k in lower if "program" in k or "regime" in k), None)
    date_col = next((lower[k] for k in lower if "date" in k or "listed" in k), None)

    out = []
    for row in reader:
        imo = re.sub(r"\D", "", str(row.get(imo_col) or ""))
        if not valid_imo(imo):
            continue
        out.append({"imo": imo,
                    "name": _clean(row.get(name_col)) if name_col else None,
                    "program": row.get(prog_col) if prog_col else None,
                    "listed_on": row.get(date_col) if date_col else None,
                    "basis": "direct", "via": None, "raw": json.dumps(row)})
    return out


def detect_format(text: str, path: str, authority: str) -> str:
    """OFAC's SDN.CSV has no header row, which silently breaks DictReader."""
    if not path.lower().endswith(".csv"):
        return "generic"
    try:
        cols = next(csv.reader(io.StringIO(text.lstrip().split("\n", 1)[0])))
    except StopIteration:
        return "generic"
    # A header row never begins with a bare integer; every OFAC data row does.
    # Testing cell *content* for words like "vessel" fails, because those words
    # appear inside the data too.
    headerless = (cols[0] if cols else "").strip().strip('"').isdigit()
    if headerless and (len(cols) == 12 or authority.upper() == "OFAC"):
        return "ofac"
    return "csv"


def _clean(value):
    if not value:
        return None
    return re.sub(r"\s+", " ", str(value)).strip(" ,;\"'")[:120] or None


# ---------------------------------------------------------------------------
def store(conn, records, authority, source, include_linked=False):
    """Upsert designations. Returns (new, updated, skipped_linked)."""
    fetched = iso(now_utc())
    new = updated = skipped = 0
    for rec in records:
        if rec["basis"] == "linked" and not include_linked:
            skipped += 1
            continue
        existing = conn.execute(
            "SELECT basis FROM designations WHERE imo=? AND authority=?",
            (rec["imo"], authority)).fetchone()
        raw = rec["raw"]
        if rec.get("via"):
            raw = json.dumps({"via_entity": rec["via"], "record": rec["raw"]})
        if existing is None:
            conn.execute(
                """INSERT INTO designations (imo, name, authority, basis, program,
                                             listed_on, source, fetched_at, raw)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (rec["imo"], rec["name"], authority, rec["basis"], rec["program"],
                 rec["listed_on"], source, fetched, raw))
            new += 1
        else:
            # Never downgrade a direct designation to a mere mention.
            basis = (rec["basis"] if BASIS_RANK[rec["basis"]]
                     > BASIS_RANK[existing["basis"]] else existing["basis"])
            conn.execute(
                """UPDATE designations SET
                       name = COALESCE(?, name), program = COALESCE(?, program),
                       basis = ?, fetched_at = ?
                   WHERE imo = ? AND authority = ?""",
                (rec["name"], rec["program"], basis, fetched,
                 rec["imo"], authority))
            updated += 1
    conn.commit()
    return new, updated, skipped


def export(conn, path):
    rows = conn.execute(
        """SELECT imo, MAX(name) AS name,
                  GROUP_CONCAT(DISTINCT authority) AS authorities,
                  GROUP_CONCAT(DISTINCT basis)     AS bases,
                  GROUP_CONCAT(DISTINCT program)   AS programs
           FROM designations GROUP BY imo ORDER BY imo""").fetchall()
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["imo", "name", "authorities", "bases", "programs"])
        for r in rows:
            w.writerow([r["imo"], r["name"] or "", r["authorities"],
                        r["bases"], r["programs"] or ""])
    print(f"exported {len(rows)} vessels -> {path}")


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="shadowfleet sanctions", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=db.DEFAULT_DB)
    ap.add_argument("--source", choices=["ofac", "eu", "uk", "all"], default="all")
    ap.add_argument("--file", help="parse a local file instead of downloading")
    ap.add_argument("--authority", default="OTHER",
                    help="authority label to use with --file")
    ap.add_argument("--format", choices=["auto", "ofac", "csv", "generic"],
                    default="auto", help="parser to use with --file")
    ap.add_argument("--include-linked", action="store_true",
                    help="also store IMOs merely mentioned in another entity's "
                         "remarks (off by default: a mention is not a designation)")
    ap.add_argument("--export", help="write the merged watchlist to this CSV")
    args = ap.parse_args(argv)

    conn = db.connect(args.db)
    parsers = {"ofac": parse_ofac, "csv": parse_csv_file, "generic": parse_generic}

    if args.file:
        text = open(args.file, encoding="utf-8", errors="replace").read()
        fmt = args.format
        if fmt == "auto":
            fmt = detect_format(text, args.file, args.authority)
            print(f"detected format: {fmt}")
        recs = parsers[fmt](text)
        n, u, s = store(conn, recs, args.authority.upper(), args.file,
                        args.include_linked)
        print(f"{args.authority.upper()}: {len(recs)} parsed, "
              f"{n} new, {u} updated, {s} linked-but-skipped")
    else:
        for src in (["ofac", "eu", "uk"] if args.source == "all" else [args.source]):
            print(f"[{src.upper()}]")
            try:
                text, url = fetch(SOURCES[src])
            except Exception as exc:    # noqa: BLE001
                print(f"  skipped: {exc}\n  -> download manually and re-run with "
                      f"--file <path> --authority {src.upper()}")
                continue
            recs = parse_ofac(text) if src == "ofac" else parse_generic(text)
            n, u, s = store(conn, recs, src.upper(), url, args.include_linked)
            print(f"  {len(recs)} parsed, {n} new, {u} updated, "
                  f"{s} linked-but-skipped")

    counts = conn.execute(
        "SELECT basis, COUNT(DISTINCT imo) c FROM designations GROUP BY basis"
    ).fetchall()
    total = conn.execute(
        "SELECT COUNT(DISTINCT imo) c FROM designations").fetchone()["c"]
    detail = ", ".join(f"{r['c']} {r['basis']}" for r in counts)
    print(f"\ndesignated vessels in database: {total}" + (f" ({detail})" if detail else ""))
    if args.export:
        export(conn, args.export)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
