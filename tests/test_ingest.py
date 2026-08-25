"""Archive ingest. Fabricating a timestamp destroys a voyage silently."""
from __future__ import annotations

import pytest

from shadowfleet import db as sfdb
from shadowfleet.ingest import import_csv, main

BARE = "!AIVDM,1,1,,A,15M67FC000G?ufbE`FepT@3n00Sa,0*5C\n"
TAGGED = "\\s:R1,c:{epoch}*00\\" + BARE.strip() + "\n"

DMA_CSV = (
    "# Timestamp,Type of mobile,MMSI,Latitude,Longitude,Navigational status,"
    "ROT,SOG,COG,Heading,IMO,Callsign,Name,Ship type\n"
    "01/05/2024 12:00:00,Class A,219000001,55.5,11.0,Under way using engine,"
    "0,12.5,180,181,9000015,OZAA,TEST SHIP,Tanker\n"
    "01/05/2024 12:05:00,Class A,219000001,55.6,11.1,At anchor,"
    "0,0.1,180,181,9000015,OZAA,TEST SHIP,Tanker\n"
)


def test_dma_csv_columns_and_dayfirst_dates(tmp_path):
    path = tmp_path / "dma.csv"
    path.write_text(DMA_CSV)
    conn = sfdb.connect(str(tmp_path / "d.db"), verbose=False)
    import_csv(str(path), conn, {}, "dma", 1000, False, dayfirst=True)
    rows = conn.execute("SELECT ts, sog, nav_status FROM positions ORDER BY ts").fetchall()
    assert [r["ts"] for r in rows] == ["2024-05-01T12:00:00Z", "2024-05-01T12:05:00Z"]
    assert rows[1]["nav_status"] == 1                # "At anchor" mapped from text
    assert conn.execute("SELECT imo FROM vessels").fetchone()[0] == "9000015"
    conn.close()


def test_tankers_only_filter_reads_text_ship_types(tmp_path):
    path = tmp_path / "dma.csv"
    path.write_text(DMA_CSV.replace("Tanker", "Cargo"))
    conn = sfdb.connect(str(tmp_path / "d.db"), verbose=False)
    import_csv(str(path), conn, {}, "dma", 1000, True, dayfirst=True)
    assert conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 0
    conn.close()


def test_undated_nmea_archive_is_refused(tmp_path):
    """v1 stamped every sentence with the import time, collapsing an entire
    voyage into a single row via UNIQUE(mmsi, ts)."""
    pytest.importorskip("pyais")
    path = tmp_path / "bare.nmea"
    path.write_text(BARE * 200)
    dbfile = str(tmp_path / "n.db")
    with pytest.raises(SystemExit) as exc:
        main([str(path), "--format", "nmea", "--db", dbfile])
    assert "no timestamp" in str(exc.value)
    conn = sfdb.connect(dbfile, verbose=False)
    assert conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 0
    conn.close()


def test_tag_block_timestamps_preserve_chronology(tmp_path):
    pytest.importorskip("pyais")
    path = tmp_path / "tagged.nmea"
    path.write_text("".join(TAGGED.format(epoch=1671620143 + i * 60)
                            for i in range(5)))
    dbfile = str(tmp_path / "n.db")
    main([str(path), "--format", "nmea", "--db", dbfile])
    conn = sfdb.connect(dbfile, verbose=False)
    ts = [r[0] for r in conn.execute("SELECT ts FROM positions ORDER BY ts")]
    assert len(ts) == 5 and len(set(ts)) == 5
    conn.close()
