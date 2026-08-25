"""Unified command-line entry point: ``shadowfleet <command> [options]``."""
from __future__ import annotations

import importlib
import os
import sys

from . import __version__

COMMANDS = {
    "sanctions": ("shadowfleet.sanctions", "sync official designation lists"),
    "collect":   ("shadowfleet.collect", "stream live AIS from aisstream.io"),
    "ingest":    ("shadowfleet.ingest", "bulk-load archived AIS (CSV or NMEA)"),
    "gaps":      ("shadowfleet.detect_gaps", "detect AIS transmission gaps"),
    "sts":       ("shadowfleet.detect_sts", "detect ship-to-ship rendezvous"),
    "spoof":     ("shadowfleet.detect_spoof", "detect spoofing and identity swaps"),
    "risk":      ("shadowfleet.risk", "rank vessels by combined risk score"),
    "report":    ("shadowfleet.report", "build the HTML/GeoJSON/CSV brief"),
    "testdata":  ("shadowfleet.testdata", "generate a synthetic fleet for testing"),
}

USAGE = f"""shadowfleet {__version__} - OSINT pipeline for shadow-fleet monitoring

usage: shadowfleet <command> [options]

commands:
""" + "\n".join(f"  {name:<11} {desc}" for name, (_, desc) in COMMANDS.items()) + """

Run 'shadowfleet <command> --help' for command options.
Pipeline order: sanctions -> collect|ingest -> gaps, sts, spoof -> risk -> report
"""


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    if argv[0] in ("-V", "--version"):
        print(__version__)
        return 0
    command = argv[0]
    if command not in COMMANDS:
        print(f"unknown command: {command}\n", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        return 2
    module = importlib.import_module(COMMANDS[command][0])
    try:
        return module.main(argv[1:]) or 0
    except BrokenPipeError:
        # Every command prints a ranked list, so piping into head/less is
        # normal usage. Python otherwise reports the closed pipe as an
        # unhandled traceback at interpreter shutdown.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 141
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
