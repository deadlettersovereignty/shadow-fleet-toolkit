"""Sanctions ingest. Attribution errors here are the most consequential the
toolkit can make: they put undesignated hulls on a sanctions list."""
from __future__ import annotations

from shadowfleet.ids import extract_imos, valid_imo
from shadowfleet.sanctions import detect_format, parse_csv_file, parse_ofac, store

# Row 1 is a designated company listing two vessel IMOs in its remarks.
# Row 2 is a vessel designated in its own right.
OFAC_FIXTURE = (
    '2001,"EXAMPLE MANAGEMENT LLC","-0- ","RUSSIA-EO14024","-0- ","-0- ",'
    '"-0- ","-0- ","-0- ","-0- ","-0- ","Fleet includes IMO 9000015, IMO 9000027."\n'
    '2002,"EXAMPLE TANKER","vessel","RUSSIA-EO14024","-0- ","V7AB1","Crude",'
    '"110000","-0- ","Gabon","EXAMPLE MANAGEMENT LLC",'
    '"Vessel Registration Identification IMO 9000039."\n'
)


def test_headerless_ofac_is_not_read_as_a_header_row():
    assert detect_format(OFAC_FIXTURE, "sdn.csv", "OFAC") == "ofac"


def test_csv_with_header_routes_to_the_csv_parser():
    text = "imo,name,program\n9000039,SOME TANKER,RUSSIA\n"
    assert detect_format(text, "list.csv", "UK") == "csv"
    assert parse_csv_file(text)[0]["imo"] == "9000039"


def test_company_name_is_not_attached_to_its_fleet():
    """v1 labelled every IMO in a company's remarks with the company's name."""
    recs = {r["imo"]: r for r in parse_ofac(OFAC_FIXTURE)}
    assert recs["9000015"]["name"] is None
    assert recs["9000015"]["basis"] == "linked"
    assert recs["9000039"]["name"] == "EXAMPLE TANKER"
    assert recs["9000039"]["basis"] == "direct"


def test_mentions_are_not_stored_as_designations_by_default(conn):
    new, updated, skipped = store(conn, parse_ofac(OFAC_FIXTURE), "OFAC", "t")
    assert (new, updated, skipped) == (1, 0, 2)
    stored = {r["imo"] for r in conn.execute("SELECT imo FROM designations")}
    assert stored == {"9000039"}


def test_include_linked_opt_in(conn):
    store(conn, parse_ofac(OFAC_FIXTURE), "OFAC", "t", include_linked=True)
    bases = dict(conn.execute("SELECT imo, basis FROM designations"))
    assert bases == {"9000015": "linked", "9000027": "linked", "9000039": "direct"}


def test_new_versus_updated_counts_are_honest(conn):
    """v1 reported every row as 'new' on every run, so a scheduled job could
    never tell that a designation had actually been added."""
    assert store(conn, parse_ofac(OFAC_FIXTURE), "OFAC", "t")[:2] == (1, 0)
    assert store(conn, parse_ofac(OFAC_FIXTURE), "OFAC", "t")[:2] == (0, 1)


def test_direct_designation_is_never_downgraded_to_linked(conn):
    direct = [{"imo": "9000039", "name": "T", "program": None, "listed_on": None,
               "basis": "direct", "via": None, "raw": "{}"}]
    linked = [{"imo": "9000039", "name": None, "program": None, "listed_on": None,
               "basis": "linked", "via": "CO", "raw": "{}"}]
    store(conn, direct, "OFAC", "t")
    store(conn, linked, "OFAC", "t", include_linked=True)
    assert conn.execute("SELECT basis FROM designations").fetchone()[0] == "direct"


def test_imo_check_digit():
    assert valid_imo("9000015") and not valid_imo("9000016")


def test_bare_numbers_below_the_plausible_range_are_rejected():
    """1234567 satisfies the IMO check digit by chance; a passport number in a
    remarks field must not become a vessel."""
    assert valid_imo("1234567")
    assert extract_imos("DOB 01 Jan 1970; Passport 1234567.") == []
    assert extract_imos("Vessel Registration Identification IMO 9000039.") == ["9000039"]
