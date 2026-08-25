# Contributing

```bash
git clone https://github.com/USERNAME/shadow-fleet-toolkit
cd shadow-fleet-toolkit
python -m venv .venv && source .venv/bin/activate
pip install -e ".[all,dev]"
pytest && ruff check src tests
```

## Especially welcome

**Zone corrections.** `src/shadowfleet/zones.py` holds approximate centres and
generous radii for terminals, STS anchorages and chokepoints. Anchorage usage
shifts constantly and this file goes stale. If you have charted a location
properly, a PR correcting it is worth more than most code changes. Say how you
established the coordinates.

**Risk weights.** The numbers in `risk.py` are defensible starting points, not
a standard. Argue with them in an issue — with reasoning, ideally against real
data.

**New detectors.** Draught deltas and port-call inference are the two highest
value additions; see the README's Extending section.

## Ground rules for detector changes

Any change to a detector needs a test against the synthetic fleet in
`testdata.py`, and it must assert **both** directions: that the behaviour is
caught, and that `TEST FOXTROT` — the control — stays clean. A detector that
fires on everything is worse than no detector in an investigative context.

If you add a signal, add a fixture vessel exhibiting it.

## Data

Never commit AIS captures, working databases, or generated reports; `.gitignore`
covers the usual paths. Vessel data is cheap to re-fetch and awkward to have in
a public history.
