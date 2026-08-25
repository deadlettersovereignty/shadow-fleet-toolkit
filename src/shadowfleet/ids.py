"""Vessel identifier utilities: IMO checksum, MMSI -> flag state, flag risk."""
from __future__ import annotations

import re

IMO_RE = re.compile(r"\b(\d{7})\b")
IMO_PREFIXED_RE = re.compile(
    r"(?:IMO|Vessel\s+Registration\s+Identification)[\s:#no.-]{0,6}(\d{7})\b",
    re.IGNORECASE)

# Lowest number ever assigned under the Lloyd's/IMO scheme still afloat.
# A bare 7-digit number below this is almost certainly something else -
# a passport, a tax ID, a phone number - even if it passes the check digit.
PLAUSIBLE_IMO_MIN = 5_000_000


def valid_imo(value) -> bool:
    """IMO numbers carry a check digit. This kills most regex false positives.

    Digits 1-6 are weighted 7,6,5,4,3,2; the last digit of the sum must equal
    digit 7.
    """
    s = re.sub(r"\D", "", str(value or ""))
    if len(s) != 7:
        return False
    total = sum(int(s[i]) * (7 - i) for i in range(6))
    return total % 10 == int(s[6])


def extract_imos(text: str, require_prefix: bool = False):
    """Pull IMO numbers out of free text (e.g. an OFAC 'Remarks' field).

    Numbers written with an explicit IMO marker are taken at face value.
    Bare 7-digit numbers are only accepted as a fallback, and only if they
    pass the check digit AND sit in a plausible range - the check digit alone
    is too weak a filter, since roughly one in ten arbitrary 7-digit strings
    satisfies it by chance.
    """
    text = text or ""
    seen, out = set(), []

    for m in IMO_PREFIXED_RE.finditer(text):
        num = m.group(1)
        if valid_imo(num) and num not in seen:
            seen.add(num)
            out.append(num)
    if out or require_prefix:
        return out

    for m in IMO_RE.finditer(text):
        num = m.group(1)
        if num in seen or not valid_imo(num):
            continue
        if int(num) < PLAUSIBLE_IMO_MIN:
            continue
        seen.add(num)
        out.append(num)
    return out


# ---------------------------------------------------------------------------
# MMSI Maritime Identification Digits -> flag state.
# Partial list covering the states that matter most for this work. The full
# authoritative table is published by the ITU; extend as needed.
# ---------------------------------------------------------------------------
MID = {
    "201": "Albania", "205": "Belgium", "209": "Cyprus", "210": "Cyprus",
    "212": "Cyprus", "211": "Germany", "213": "Georgia", "214": "Moldova",
    "215": "Malta", "216": "Armenia", "218": "Germany", "219": "Denmark",
    "220": "Denmark", "224": "Spain", "225": "Spain", "226": "France",
    "227": "France", "228": "France", "230": "Finland", "231": "Faroe Islands",
    "232": "United Kingdom", "233": "United Kingdom", "234": "United Kingdom",
    "235": "United Kingdom", "236": "Gibraltar", "237": "Greece",
    "238": "Croatia", "239": "Greece", "240": "Greece", "241": "Greece",
    "242": "Morocco", "243": "Hungary", "244": "Netherlands",
    "245": "Netherlands", "246": "Netherlands", "247": "Italy",
    "248": "Malta", "249": "Malta", "250": "Ireland", "251": "Iceland",
    "252": "Liechtenstein", "253": "Luxembourg", "254": "Monaco",
    "255": "Portugal (Madeira)", "256": "Malta", "257": "Norway",
    "258": "Norway", "259": "Norway", "261": "Poland", "262": "Montenegro",
    "263": "Portugal", "264": "Romania", "265": "Sweden", "266": "Sweden",
    "267": "Slovakia", "268": "San Marino", "269": "Switzerland",
    "270": "Bulgaria", "271": "Turkiye", "272": "Ukraine", "273": "Russia",
    "274": "North Macedonia", "275": "Latvia", "276": "Estonia",
    "277": "Lithuania", "278": "Slovenia", "279": "Serbia",
    "301": "Anguilla", "303": "USA (Alaska)", "304": "Antigua & Barbuda",
    "305": "Antigua & Barbuda", "306": "Curacao/Sint Maarten", "307": "Aruba",
    "308": "Bahamas", "309": "Bahamas", "310": "Bermuda", "311": "Bahamas",
    "312": "Belize", "314": "Barbados", "316": "Canada", "319": "Cayman Islands",
    "321": "Costa Rica", "323": "Cuba", "325": "Dominica",
    "327": "Dominican Republic", "329": "Guadeloupe", "330": "Grenada",
    "331": "Greenland", "332": "Guatemala", "334": "Honduras", "336": "Haiti",
    "338": "United States", "339": "Jamaica", "341": "St Kitts & Nevis",
    "343": "St Lucia", "345": "Mexico", "347": "Martinique",
    "348": "Montserrat", "350": "Nicaragua", "351": "Panama", "352": "Panama",
    "353": "Panama", "354": "Panama", "355": "Panama", "356": "Panama",
    "357": "Panama", "358": "Puerto Rico", "359": "El Salvador",
    "361": "St Pierre & Miquelon", "362": "Trinidad & Tobago",
    "364": "Turks & Caicos", "366": "United States", "367": "United States",
    "368": "United States", "369": "United States", "370": "Panama",
    "371": "Panama", "372": "Panama", "373": "Panama", "374": "Panama",
    "375": "St Vincent & Grenadines", "376": "St Vincent & Grenadines",
    "377": "St Vincent & Grenadines", "378": "British Virgin Islands",
    "379": "US Virgin Islands",
    "401": "Afghanistan", "403": "Saudi Arabia", "405": "Bangladesh",
    "408": "Bahrain", "410": "Bhutan", "412": "China", "413": "China",
    "414": "China", "416": "Taiwan", "417": "Sri Lanka", "419": "India",
    "422": "Iran", "423": "Azerbaijan", "425": "Iraq", "428": "Israel",
    "431": "Japan", "432": "Japan", "434": "Turkmenistan", "436": "Kazakhstan",
    "437": "Uzbekistan", "438": "Jordan", "440": "South Korea",
    "441": "South Korea", "443": "Palestine", "445": "North Korea",
    "447": "Kuwait", "450": "Lebanon", "451": "Kyrgyzstan", "453": "Macao",
    "455": "Maldives", "457": "Mongolia", "459": "Nepal", "461": "Oman",
    "463": "Pakistan", "466": "Qatar", "468": "Syria", "470": "UAE",
    "471": "UAE", "472": "Tajikistan", "473": "Yemen", "475": "Yemen",
    "477": "Hong Kong", "478": "Bosnia & Herzegovina",
    "501": "Adelie Land", "503": "Australia", "506": "Myanmar",
    "508": "Brunei", "510": "Micronesia", "511": "Palau",
    "512": "New Zealand", "514": "Cambodia", "515": "Cambodia",
    "516": "Christmas Island", "518": "Cook Islands", "520": "Fiji",
    "523": "Cocos Islands", "525": "Indonesia", "529": "Kiribati",
    "531": "Laos", "533": "Malaysia", "536": "N Mariana Islands",
    "538": "Marshall Islands", "540": "New Caledonia", "542": "Niue",
    "544": "Nauru", "546": "French Polynesia", "548": "Philippines",
    "553": "Papua New Guinea", "555": "Pitcairn", "557": "Solomon Islands",
    "559": "American Samoa", "561": "Samoa", "563": "Singapore",
    "564": "Singapore", "565": "Singapore", "566": "Singapore",
    "567": "Thailand", "570": "Tonga", "572": "Tuvalu", "574": "Vietnam",
    "576": "Vanuatu", "577": "Vanuatu", "578": "Wallis & Futuna",
    "601": "South Africa", "603": "Angola", "605": "Algeria", "607": "Saint Paul",
    "608": "Ascension Island", "609": "Burundi", "610": "Benin",
    "611": "Botswana", "612": "Central African Rep", "613": "Cameroon",
    "615": "Congo", "616": "Comoros", "617": "Cabo Verde",
    "619": "Cote d'Ivoire", "620": "Comoros", "621": "Djibouti",
    "622": "Egypt", "624": "Ethiopia", "625": "Eritrea", "626": "Gabon",
    "627": "Ghana", "629": "Gambia", "630": "Guinea-Bissau",
    "631": "Equatorial Guinea", "632": "Guinea", "633": "Burkina Faso",
    "634": "Kenya", "636": "Liberia", "637": "Liberia", "642": "Libya",
    "644": "Lesotho", "645": "Mauritius", "647": "Madagascar", "649": "Mali",
    "650": "Mozambique", "654": "Mauritania", "655": "Malawi", "656": "Niger",
    "657": "Nigeria", "659": "Namibia", "660": "Reunion", "661": "Rwanda",
    "662": "Sudan", "663": "Senegal", "664": "Seychelles",
    "666": "Somalia", "667": "Sierra Leone", "668": "Sao Tome & Principe",
    "669": "Eswatini", "670": "Chad", "671": "Togo", "672": "Tunisia",
    "674": "Tanzania", "675": "Uganda", "676": "DR Congo", "677": "Tanzania",
    "678": "Zambia", "679": "Zimbabwe",
    "701": "Argentina", "710": "Brazil", "720": "Bolivia", "725": "Chile",
    "730": "Colombia", "735": "Ecuador", "740": "Falkland Islands",
    "745": "Guiana", "750": "Guyana", "755": "Paraguay", "760": "Peru",
    "765": "Suriname", "770": "Uruguay", "775": "Venezuela",
}

# Registries repeatedly observed hosting sanctioned or opaque tanker tonnage,
# plus registries that are small, newly commercialised, or have been formally
# disowned by the state in question. Presence here is a triage hint, NOT an
# accusation - large numbers of entirely legitimate ships fly these flags.
ELEVATED_RISK_FLAGS = {
    "Cameroon", "Comoros", "Cook Islands", "Gabon", "Guyana", "Palau",
    "Sao Tome & Principe", "Sierra Leone", "Eswatini", "Tanzania", "Togo",
    "Barbados", "Djibouti", "Mongolia", "Honduras", "Guinea-Bissau",
    "Benin", "San Marino", "Niue", "Vanuatu", "Belize",
}


def flag_from_mmsi(mmsi) -> str | None:
    s = re.sub(r"\D", "", str(mmsi or ""))
    if len(s) != 9:
        return None
    return MID.get(s[:3])


def is_elevated_risk_flag(flag) -> bool:
    return bool(flag) and flag in ELEVATED_RISK_FLAGS


def mmsi_looks_valid(mmsi) -> bool:
    s = re.sub(r"\D", "", str(mmsi or ""))
    return len(s) == 9 and s[0] in "23456789" and MID.get(s[:3]) is not None
