"""Report rendering. AIS ship names are attacker-controlled free text."""
from __future__ import annotations

import json

from shadowfleet import db as sfdb
from shadowfleet import report

PAYLOAD = 'NEVA</script><script>alert(document.domain)</script>'


def test_js_json_cannot_close_the_script_tag():
    out = report.js_json({"name": PAYLOAD})
    assert "</script>" not in out
    assert "<" not in out and ">" not in out


def test_js_json_round_trips_unchanged():
    obj = {"name": PAYLOAD, "sep": "a\u2028b", "amp": "x&y"}
    assert json.loads(report.js_json(obj)) == obj


def test_hostile_name_does_not_escape_the_script_block(conn, tmp_path):
    sfdb.upsert_vessel(conn, "538999901", "2026-06-01T00:00:00Z",
                       imo="9000015", name=PAYLOAD)
    sfdb.insert_event(conn, "gap", "538999901", "2026-06-01T00:00:00Z",
                      imo="9000015", name=PAYLOAD, end_ts="2026-06-02T00:00:00Z",
                      lat=60.0, lon=28.0, lat2=57.0, lon2=11.0, severity=80.0,
                      detail={"hours": 24.0})
    conn.commit()
    out = tmp_path / "r.html"
    report.main(["--db", conn.execute("PRAGMA database_list").fetchone()[2],
                 "--out", str(out)])
    html = out.read_text()
    body = html.split("const data =", 1)[1]
    assert "<script>alert" not in body
    assert "&lt;/script&gt;" in html          # table cell escaped the normal way


def test_mirror_rows_excluded_by_default(fleet, tmp_path):
    rows = report.fetch_events(fleet, None, 0.0, None, 500)
    assert all(r["subkind"] != "mirror" for r in rows)
    with_mirrors = report.fetch_events(fleet, None, 0.0, None, 500,
                                       include_mirrors=True)
    assert len(with_mirrors) > len(rows)
