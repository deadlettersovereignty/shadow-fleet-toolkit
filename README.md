# PENUMBRA: A Shadow Fleet toolkit

[![CI](https://github.com/deadlettersovereignty/shadow-fleet-toolkit/actions/workflows/ci.yml/badge.svg)](https://github.com/deadlettersovereignty/shadow-fleet-toolkit/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-Apache--2.0-green)](LICENSE)

Created for my blog, this is an OSINT pipeline for monitoring sanctioned and sanctions-evading tanker
traffic: pull official designation lists, ingest AIS, detect the behaviours
associated with evasion, score vessels, and produce a mapped brief.

Built for sanctions-compliance screening, journalism, and maritime research.
Everything it uses is public: government sanctions lists and AIS broadcasts.

```bash
pip install shadow-fleet-toolkit[all]

shadowfleet testdata --db demo.db      # synthetic fleet, no API key needed
shadowfleet gaps  --db demo.db --min-hours 4
shadowfleet sts   --db demo.db
shadowfleet spoof --db demo.db
shadowfleet risk  --db demo.db
shadowfleet report --db demo.db --out demo.html
```

## Pipeline

One SQLite file carries everything. Each stage is idempotent, so re-running is
always safe.

| Step | Command | What it does |
|---|---|---|
| 1 | `shadowfleet sanctions` | Downloads OFAC / EU / UK lists, extracts vessel IMO numbers, builds the watchlist |
| 2 | `shadowfleet collect` | Streams live AIS from aisstream.io |
| 2b | `shadowfleet ingest` | Bulk-loads archived AIS (CSV or NMEA) |
| 3 | `shadowfleet gaps` | AIS transmission gaps — "going dark" |
| 4 | `shadowfleet sts` | Ship-to-ship rendezvous |
| 5 | `shadowfleet spoof` | Position spoofing and identity laundering |
| 6 | `shadowfleet risk` | Combines every signal into one ranked score per vessel |
| 7 | `shadowfleet report` | HTML brief with a Leaflet map, plus GeoJSON and CSV |

The synthetic fleet from `shadowfleet testdata` plants one dark-period vessel,
an STS pair, a spoofed teleport, a circling spoofer, an identity swap, and one
clean control ship — so you can confirm each detector fires and, just as
importantly, that the control does not.

### A real run

```bash
shadowfleet sanctions --source all --export watchlist.csv
export AISSTREAM_API_KEY=...           # free key at aisstream.io
shadowfleet collect --region baltic black_sea med_east
# ...let it run...
shadowfleet gaps  --min-hours 6
shadowfleet sts   --since 2026-06-01
shadowfleet spoof
shadowfleet risk  --enrich fleet_metadata.csv --export risk.csv
shadowfleet report --min-severity 40 --geojson events.geojson
```

## Installation

```bash
pip install shadow-fleet-toolkit          # core: sanctions sync + all detectors
pip install shadow-fleet-toolkit[live]    # + live AIS collection
pip install shadow-fleet-toolkit[nmea]    # + raw NMEA decoding
pip install shadow-fleet-toolkit[all]
```

Python 3.10+. The detectors themselves need nothing beyond the standard library.

## Data sources

**Sanctions lists.** We're rocking with the classics: OFAC SDN, the EU consolidated list and the UK list are all
free. Download URLs move; every source can be fed from a local file instead
(`--file path --authority OFAC`), which is the more reliable choice for a
scheduled job.

**AIS.** Hooray for open-source maritime data. [aisstream.io](https://aisstream.io) offers a free websocket feed. For
history rather than live capture, the
[Danish Maritime Authority](https://web.ais.dk/aisdata/) publishes free daily
AIS covering the Baltic and Danish Straits — the single most useful free
archive for this subject, since most Russian Baltic crude exports transit it.
NOAA Marine Cadastre covers US waters. Commercial providers (Spire, Kpler,
Windward, MarineTraffic) add satellite AIS, which matters because terrestrial
receivers only reach 40–60 nm offshore.

**Pitfalls of AIS.** Various things are missing, despite the strength of the AIS-forward approach: these are uild year, deadweight, beneficial ownership,
management, P&I cover and classification society all have to come from a
registry — Equasis (free, registration required), IHS, or Lloyd's List
Intelligence. Feed them in via `shadowfleet risk --enrich`. You'll need to do this research yourself, sorry big guy.

## "How Do I Work This Thing?!": Reading the output

Each detector emits a severity score. These are triage priorities, not
verdicts, and the weights in `risk.py` are judgement calls — publish them
alongside any finding so readers can argue with the arithmetic rather than just
the conclusion.

Things worth keeping in mind before acting on a detection:

**Do your homework.** Please. This database alone is not in any way indication of Shadow Fleet status, merely a place to start.

**AIS is unauthenticated.** It is an unencrypted VHF broadcast with no signing.
Position, identity and status are whatever the transmitter claims. Unfortunately, we just gotta deal with this fact.

**Most gaps tend to be boring.** Lock in, most of the pauses to transponder status are neutral

**Proximity is not transfer.** Tugs, bunker barges, pilot boats and vessels
anchored on the same tide all reproduce the STS signature. Corroborate with
draught changes, duration, and imagery where you can get it. I firmly suggest PlanetLabs if you have the money to task. 

**Identity signals are pretty strong.** One IMO broadcasting under multiple
MMSIs, or an MMSI cycling through names, is hard to explain innocently and hard
to produce by accident. Weight these above gaps. I've tried to bake this into the system but it's not the easiest thing in the world.

**Careful what you wish for.** Sanctions entries frequently name vessel
IMOs inside an owner's or manager's remarks. Those are recorded with
`basis = 'linked'` and excluded unless you pass `--include-linked`, because
treating a mention as a listing puts undesignated hulls on a sanctions list. Don't go poking around with vessel owners you wouldn't want to meet in a dark boardroom.

**False designation is not really the same as "shadow fleet".** Most tankers flying a
convenience flag are engaged in entirely ordinary trade, and an old ship is
just an old ship. Again, do your homework.

**Check the ZONE definitions.** `zones.py` holds approximate centres and
generous radii. Anchorage usage shifts constantly. Verify against a chart
before publishing, and maintain the file as you learn.
Welcome to the ZONE.

## Layout

```
src/shadowfleet/
  geo.py       distance, bearing, timestamp parsing
  db.py        SQLite schema, migrations, access helpers
  zones.py     terminals, STS anchorages, chokepoints
  ids.py       IMO check digit, MMSI -> flag state, flag risk
  sanctions.py designation-list ingest
  collect.py   live AIS collection
  ingest.py    archived AIS (CSV / NMEA)
  detect_*.py  gap, STS and spoofing detectors
  risk.py      composite scoring
  report.py    HTML / GeoJSON / CSV output
```

## Notes on the implementation

`detect_sts.py` snaps positions to time bins and a spatial grid, so only
vessels in adjacent cells are ever compared; naive pairwise comparison is
O(n²) and will not finish on real data. The grid is sized from `--max-distance`
and the highest latitude in the data — a fixed cell size silently misses pairs
as soon as the radius is widened or the fleet moves north. Bins are processed
as they stream out of SQLite, so memory is bounded by the busiest bin.

`ids.py` validates IMO check digits, but the check digit alone is a weak
filter: roughly one in ten arbitrary 7-digit strings passes it. `extract_imos()`
therefore prefers numbers written with an explicit IMO marker and only falls
back to bare digits inside a plausible range. That is what stops passport
numbers in an OFAC remarks field being ingested as vessels.

Timestamps are the most dangerous surface in the whole toolkit, because a
mis-parsed one does not raise — it fabricates gaps and destroys co-location.
Every accepted format is pinned in `tests/test_geo.py`.

## Extending

Roughly in order of value:

- **Draught deltas.** AIS static messages carry reported draught. A laden →
  ballast change without an intervening port call is strong STS corroboration,
  and the schema already stores the field.
- **Port call inference.** Cluster stationary periods inside terminal zones.
- **Satellite AIS.** Terrestrial-only coverage is why open-ocean gaps are
  ambiguous.
- **SAR imagery.** Sentinel-1 is free via Copernicus and detects hulls that are
  not transmitting at all — the one source that closes the dark-gap loop.
- **Registry scraping.** Equasis gives flag history and P&I cover, which turns
  flag-hopping from an inference into a record.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Corrections to `zones.py` are
especially welcome. I welcome assistance - 90% of the code here is held together with spit and string. If you see something that makes too much sense, assume Claude helped. Well, everybody's gotta learn sometime... 

## License

Apache-2.0. See [LICENSE](LICENSE).

This is a research tool operating on public data. It produces leads, not
conclusions. Verify before you publish.
