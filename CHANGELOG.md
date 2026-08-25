# Changelog

## 0.2.0

Restructured into an installable package with a single `shadowfleet` command.
Everything below was found by stress-testing 0.1.0 against adversarial and
realistic inputs rather than the synthetic fleet alone.

### Fixed — silent data corruption

- **UTC offsets were discarded.** `parse_ts` matched an aisstream-shaped prefix
  before reaching `fromisoformat`, so `2024-05-01T12:00:00+02:00` parsed as
  12:00 UTC. Sources emitting colon offsets (NOAA Marine Cadastre among them)
  were shifted by hours, fabricating AIS gaps and destroying STS co-location.
- **The NMEA importer annihilated archives.** pyais exposes no `timestamp` key,
  so every sentence was stamped with the import clock; combined with
  `UNIQUE(mmsi, ts)` a 200-fix voyage collapsed to one row. TAG-block and
  leading-column timestamps are now used, `--live` is an explicit opt-in, and
  an undated archive aborts instead of importing garbage.
- **The `vessels` table grew one row per message.** `UNIQUE(mmsi, imo, name)`
  never fired when the IMO was NULL, because SQLite treats NULLs as distinct.
  Now uniquely indexed on `(mmsi, IFNULL(imo,''), IFNULL(name,''))`.
- **The STS grid missed pairs.** Cell size was hardcoded while `--max-distance`
  was tunable, so two vessels 1.9 nm apart at 60°N with `--max-distance 2.0`
  were reported as no rendezvous. The grid is now derived from the search
  radius and the worst-case latitude, and longitude cells wrap at the
  antimeridian.

### Fixed — distorted findings

- **Only one identity finding per vessel survived.** All identity events shared
  a `start_ts`, colliding on `UNIQUE(kind, mmsi, start_ts, counterpart)`.
  Events now carry a `subkind`.
- **STS was scored one-sided.** The encounter was written only against vessel
  A, so the counterparty scored as though nothing happened. Both sides are now
  recorded; the reciprocal row is marked `mirror` so reports do not double-plot.
- **Sanctions attribution.** An IMO mentioned in a designated company's remarks
  was stored as a designation carrying the *company's* name. Records now carry
  a `basis` of `direct`, `linked` or `unverified`; mentions are excluded unless
  `--include-linked` is passed, and score far lower when included.
- **XSS in the HTML report.** `json.dumps` escapes quotes but not `</script>`,
  and ship names are attacker-controlled. HTML-significant characters are now
  `\uXXXX`-escaped inside the script block.

### Fixed — operability

- `Ctrl-C` during collection hung for up to 120 s; the socket now races the
  stop event, and a second interrupt forces exit.
- Sanctions sync reported every row as "new" on every run, so a scheduled job
  could not detect an actual addition. New, updated and skipped are now
  distinguished.
- Position reports no longer trigger a vessel write per message.
- The STS detector streams bins instead of materialising the whole query
  (roughly 3 GB resident per day of Danish AIS in 0.1.0).
- Ingest reports how many rows collided on `(mmsi, ts)` and warns above 20%.

### Changed

- `TEST FOXTROT`, the control vessel, now scores 0. Synthetic MMSIs used the
  unassigned `111` prefix, tripping the identity checks and making the control
  useless as a control.
- Databases from 0.1.0 migrate automatically on first open, collapsing
  duplicated vessel rows. Re-run the detectors afterwards to recover findings
  that 0.1.0 dropped.

## 0.1.0

Initial pipeline: sanctions sync, AIS collection, gap/STS/spoofing detection,
risk scoring, HTML reporting.
