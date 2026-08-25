#!/usr/bin/env python3
"""Turn the database into a briefing: HTML map + GeoJSON + CSV.

    shadowfleet report --out report.html --geojson events.geojson
    shadowfleet report --kind sts gap --min-severity 50

The GeoJSON imports straight into QGIS or kepler.gl if you want to do real
cartography. The HTML is a single self-contained file you can email.
"""
from __future__ import annotations

import argparse
import csv
import html
import json
from collections import Counter

from . import db
from .geo import iso, now_utc

KIND_COLOUR = {"gap": "#e8833a", "sts": "#c0392b", "teleport": "#8e44ad",
               "circle": "#2980b9", "frozen": "#16a085", "identity": "#7f8c8d"}


def fetch_events(conn, kinds, min_sev, since, limit, include_mirrors=False):
    sql = "SELECT * FROM events WHERE severity >= ?"
    args = [min_sev]
    if not include_mirrors:
        # The STS detector writes each encounter from both sides so that every
        # vessel carries the finding; the mirror would otherwise plot twice.
        sql += " AND subkind <> 'mirror'"
    if kinds:
        sql += f" AND kind IN ({','.join('?' * len(kinds))})"
        args += list(kinds)
    if since:
        sql += " AND start_ts >= ?"
        args.append(since)
    sql += " ORDER BY severity DESC, start_ts DESC LIMIT ?"
    args.append(limit)
    return conn.execute(sql, args).fetchall()


def to_geojson(rows):
    feats = []
    for r in rows:
        detail = json.loads(r["detail"] or "{}")
        props = {"kind": r["kind"], "mmsi": r["mmsi"], "imo": r["imo"],
                 "name": r["name"], "start_ts": r["start_ts"], "end_ts": r["end_ts"],
                 "zone": r["zone"], "severity": r["severity"],
                 "subkind": r["subkind"] or None,
                 "counterpart": r["counterpart"] or None, **detail}
        if r["lat"] is None:
            continue
        if r["lat2"] is not None and r["kind"] == "gap":
            geom = {"type": "LineString",
                    "coordinates": [[r["lon"], r["lat"]], [r["lon2"], r["lat2"]]]}
        else:
            geom = {"type": "Point", "coordinates": [r["lon"], r["lat"]]}
        feats.append({"type": "Feature", "geometry": geom, "properties": props})
    return {"type": "FeatureCollection", "features": feats}


HTML_TMPL = """<!doctype html>
<meta charset="utf-8"><title>Shadow fleet monitoring brief</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
 body{{margin:0;font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
      color:#1a1a1a;background:#fafafa}}
 header{{padding:20px 28px;background:#14202b;color:#f5f5f5}}
 header h1{{margin:0;font-size:19px;font-weight:600;letter-spacing:.2px}}
 header p{{margin:6px 0 0;font-size:12px;opacity:.65}}
 .wrap{{padding:0 28px 40px}}
 #map{{height:460px;margin:20px 0;border:1px solid #d8d8d8}}
 .chips{{margin:16px 0}}
 .chip{{display:inline-block;padding:3px 10px;margin:0 6px 6px 0;border-radius:11px;
       font-size:12px;color:#fff}}
 table{{border-collapse:collapse;width:100%;background:#fff;font-size:13px}}
 th,td{{padding:7px 10px;border-bottom:1px solid #ececec;text-align:left;vertical-align:top}}
 th{{background:#f0f2f4;font-weight:600;position:sticky;top:0}}
 tr:hover td{{background:#fbfcfd}}
 .sev{{font-weight:600;font-variant-numeric:tabular-nums}}
 .k{{display:inline-block;padding:1px 7px;border-radius:9px;color:#fff;font-size:11px}}
 .muted{{color:#888}}
 footer{{padding:18px 28px;font-size:12px;color:#666;border-top:1px solid #e2e2e2;
         background:#f2f2f2}}
</style>
<header>
 <h1>Shadow fleet monitoring brief</h1>
 <p>Generated {generated} &middot; {n_events} events &middot; {n_vessels} vessels
    &middot; {n_designated} designated hulls in database</p>
</header>
<div class="wrap">
 <div class="chips">{chips}</div>
 <div id="map"></div>
 <h3>Top risk-ranked vessels</h3>
 {risk_table}
 <h3>Events</h3>
 {event_table}
</div>
<footer>
 AIS is unauthenticated and self-reported. Detections here are leads requiring
 corroboration &mdash; port records, registry data, imagery &mdash; before use.
 Zone boundaries are approximate.
</footer>
<script>
const data = {geojson};
const colours = {colours};
const map = L.map('map').setView([50, 20], 3);
L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',
  {{maxZoom: 17, attribution: '&copy; OpenStreetMap contributors'}}).addTo(map);
const layer = L.geoJSON(data, {{
  pointToLayer: (f, ll) => L.circleMarker(ll, {{
    radius: 4 + (f.properties.severity || 0) / 20,
    color: colours[f.properties.kind] || '#555',
    fillColor: colours[f.properties.kind] || '#555',
    fillOpacity: .65, weight: 1
  }}),
  style: f => ({{color: colours[f.properties.kind] || '#555', weight: 2, dashArray: '5,5'}}),
  onEachFeature: (f, l) => {{
    const p = f.properties;
    l.bindPopup('<b>' + (p.name || 'unknown') + '</b><br>' + p.kind.toUpperCase()
      + ' &middot; severity ' + Math.round(p.severity) + '<br>MMSI ' + p.mmsi
      + ' &middot; IMO ' + (p.imo || '?') + '<br>' + p.start_ts
      + (p.zone ? '<br>' + p.zone : ''));
  }}
}}).addTo(map);
if (data.features.length) map.fitBounds(layer.getBounds(), {{padding: [30, 30]}});
</script>
"""


def js_json(obj) -> str:
    """JSON for embedding inside a <script> block.

    json.dumps escapes quotes but not "</script>", and AIS ship names are
    attacker-controlled free text broadcast over unauthenticated VHF. Escaping
    the three HTML-significant characters as \\uXXXX keeps the value byte-identical
    after JSON.parse while making it impossible to close the tag early.
    U+2028/9 are legal in JSON strings but not in JS string literals.
    """
    return (json.dumps(obj)
            .replace("<", "\\u003c").replace(">", "\\u003e")
            .replace("&", "\\u0026")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def esc(v):
    return html.escape(str(v)) if v is not None else '<span class="muted">-</span>'


def render_events(rows):
    if not rows:
        return "<p class='muted'>No events matched the filter.</p>"
    out = ["<table><tr><th>Sev</th><th>Kind</th><th>Vessel</th><th>MMSI</th>",
           "<th>IMO</th><th>When</th><th>Zone</th><th>Detail</th></tr>"]
    for r in rows:
        d = json.loads(r["detail"] or "{}")
        summary = "; ".join(f"{k}={v}" for k, v in list(d.items())[:3]
                            if not isinstance(v, (list, dict)))
        colour = KIND_COLOUR.get(r["kind"], "#555")
        label = f"{r['kind']}/{r['subkind']}" if r["subkind"] else r["kind"]
        out.append(
            f"<tr><td class='sev'>{r['severity']:.0f}</td>"
            f"<td><span class='k' style='background:{colour}'>{esc(label)}</span></td>"
            f"<td>{esc(r['name'])}</td><td>{esc(r['mmsi'])}</td><td>{esc(r['imo'])}</td>"
            f"<td>{esc(r['start_ts'])}</td><td>{esc(r['zone'])}</td>"
            f"<td>{esc(summary)}</td></tr>")
    out.append("</table>")
    return "".join(out)


def render_risk(conn, top):
    rows = conn.execute("SELECT * FROM risk ORDER BY score DESC LIMIT ?",
                        (top,)).fetchall()
    if not rows:
        return "<p class='muted'>Run risk_score.py to populate this table.</p>"
    out = ["<table><tr><th>Score</th><th>Band</th><th>Vessel</th><th>MMSI</th>",
           "<th>IMO</th><th>Flag</th><th>Drivers</th></tr>"]
    for r in rows:
        factors = json.loads(r["factors"] or "[]")
        drivers = "; ".join(f["note"] for f in factors[:3])
        out.append(f"<tr><td class='sev'>{r['score']:.0f}</td><td>{esc(r['band'])}</td>"
                   f"<td>{esc(r['name'])}</td><td>{esc(r['mmsi'])}</td>"
                   f"<td>{esc(r['imo'])}</td><td>{esc(r['flag'])}</td>"
                   f"<td>{esc(drivers)}</td></tr>")
    out.append("</table>")
    return "".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="shadowfleet report", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=db.DEFAULT_DB)
    ap.add_argument("--out", default="report.html")
    ap.add_argument("--geojson")
    ap.add_argument("--csv")
    ap.add_argument("--kind", nargs="*",
                    choices=["gap", "sts", "teleport", "frozen", "circle", "identity"])
    ap.add_argument("--min-severity", type=float, default=0.0)
    ap.add_argument("--since")
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--top-risk", type=int, default=25)
    ap.add_argument("--include-mirrors", action="store_true",
                    help="also list the reciprocal row of each STS encounter")
    args = ap.parse_args(argv)

    conn = db.connect(args.db)
    rows = fetch_events(conn, args.kind, args.min_severity, args.since,
                        args.limit, args.include_mirrors)
    counts = Counter(r["kind"] for r in rows)
    chips = "".join(
        f"<span class='chip' style='background:{KIND_COLOUR.get(k, '#555')}'>"
        f"{k} &middot; {v}</span>" for k, v in counts.most_common())

    gj = to_geojson(rows)
    stats = {
        "n_vessels": conn.execute(
            "SELECT COUNT(DISTINCT mmsi) c FROM positions").fetchone()["c"],
        "n_designated": conn.execute(
            "SELECT COUNT(DISTINCT imo) c FROM designations").fetchone()["c"],
    }

    page = HTML_TMPL.format(
        generated=iso(now_utc()), n_events=len(rows),
        n_vessels=f"{stats['n_vessels']:,}", n_designated=f"{stats['n_designated']:,}",
        chips=chips or "<span class='muted'>no events</span>",
        risk_table=render_risk(conn, args.top_risk),
        event_table=render_events(rows),
        geojson=js_json(gj), colours=js_json(KIND_COLOUR))

    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(page)
    print(f"wrote {args.out} ({len(rows)} events, {len(gj['features'])} mapped)")

    if args.geojson:
        with open(args.geojson, "w", encoding="utf-8") as fh:
            json.dump(gj, fh, indent=1)
        print(f"wrote {args.geojson}")

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["severity", "kind", "name", "mmsi", "imo", "start_ts",
                        "end_ts", "lat", "lon", "zone", "counterpart", "detail"])
            for r in rows:
                w.writerow([r["severity"], r["kind"], r["name"] or "", r["mmsi"],
                            r["imo"] or "", r["start_ts"], r["end_ts"] or "",
                            r["lat"], r["lon"], r["zone"] or "",
                            r["counterpart"] or "", r["detail"]])
        print(f"wrote {args.csv}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
