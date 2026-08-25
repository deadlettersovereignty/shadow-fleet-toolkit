#!/usr/bin/env python3
"""Collect live AIS into SQLite from aisstream.io.

    export AISSTREAM_API_KEY=...
    shadowfleet collect --region baltic black_sea
    shadowfleet collect --watchlist
    shadowfleet collect --mmsi 273123456 636987654

Free key: https://aisstream.io/

The service caps MMSI filters at 50 per subscription. A watchlist longer than
that is truncated with a warning - collect by region and filter afterwards
instead of expecting the filter to cover the whole list.

AIS is self-reported. Everything downstream of this file inherits that.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import signal
import sys
from collections import deque

from . import db, zones
from .geo import iso, now_utc, parse_ts
from .ids import flag_from_mmsi

try:
    import websockets
except ImportError:                     # pragma: no cover
    websockets = None

ENDPOINT = "wss://stream.aisstream.io/v0/stream"
MMSI_FILTER_LIMIT = 50
IDLE_TIMEOUT = 120
POSITION_TYPES = {"PositionReport", "StandardClassBPositionReport",
                  "ExtendedClassBPositionReport"}


def build_subscription(api_key, boxes, mmsis):
    sub = {"APIKey": api_key,
           "BoundingBoxes": boxes or [zones.REGIONS["world"]],
           "FilterMessageTypes": ["PositionReport", "ShipStaticData",
                                  "StandardClassBPositionReport"]}
    if mmsis:
        sub["FiltersShipMMSI"] = [str(m) for m in mmsis[:MMSI_FILTER_LIMIT]]
    return sub


def parse_message(msg: dict):
    """Return (position_row | None, static_dict | None)."""
    mtype = msg.get("MessageType")
    meta = msg.get("MetaData") or {}
    body = (msg.get("Message") or {}).get(mtype) or {}
    mmsi = str(meta.get("MMSI") or body.get("UserID") or "").strip()
    if not mmsi:
        return None, None

    try:
        ts = iso(parse_ts(meta.get("time_utc") or now_utc()))
    except ValueError:
        ts = iso(now_utc())
    name = (meta.get("ShipName") or body.get("Name") or "").strip() or None

    if mtype in POSITION_TYPES:
        lat = body.get("Latitude", meta.get("latitude"))
        lon = body.get("Longitude", meta.get("longitude"))
        if lat is None or lon is None or abs(lat) > 90 or abs(lon) > 180:
            return None, None
        sog, heading = body.get("Sog"), body.get("TrueHeading")
        row = {"mmsi": mmsi, "ts": ts, "lat": float(lat), "lon": float(lon),
               # 102.3 kn and heading 511 are AIS "not available" sentinels.
               "sog": None if (sog is not None and sog > 102) else sog,
               "cog": body.get("Cog"),
               "heading": None if (heading is not None and heading >= 511) else heading,
               "nav_status": body.get("NavigationalStatus"),
               "draught": None, "source": "aisstream"}
        static = ({"mmsi": mmsi, "ts": ts, "name": name,
                   "flag": flag_from_mmsi(mmsi)} if name else None)
        return row, static

    if mtype == "ShipStaticData":
        imo = body.get("ImoNumber")
        dim = body.get("Dimension") or {}
        return None, {
            "mmsi": mmsi, "ts": ts,
            "imo": str(imo) if imo else None, "name": name,
            "callsign": (body.get("CallSign") or "").strip() or None,
            "ship_type": body.get("Type"), "flag": flag_from_mmsi(mmsi),
            "length_m": (dim.get("A") or 0) + (dim.get("B") or 0) or None,
            "width_m": (dim.get("C") or 0) + (dim.get("D") or 0) or None}
    return None, None


class VesselCache:
    """Suppress redundant vessel upserts.

    Position reports carry a ship name, so writing on every message means one
    UPDATE per message for no new information. Only a changed identity tuple
    is worth a write.
    """

    def __init__(self):
        self._seen = {}

    def should_write(self, static) -> bool:
        key = (static["mmsi"], static.get("imo"), static.get("name"))
        signature = (static.get("callsign"), static.get("ship_type"))
        if self._seen.get(key) == signature:
            return False
        self._seen[key] = signature
        return True


async def run(api_key, boxes, mmsis, dbpath, flush_every, verbose):
    conn = db.connect(dbpath)
    buf, cache = deque(), VesselCache()
    stats = {"pos": 0, "static": 0, "msgs": 0}
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def request_stop():
        if stop.is_set():               # second interrupt: give up immediately
            raise KeyboardInterrupt
        print("\nstopping (Ctrl-C again to force)...", file=sys.stderr)
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, AttributeError):
            loop.add_signal_handler(sig, request_stop)

    def flush():
        if buf:
            stats["pos"] += db.insert_positions(conn, list(buf))
            buf.clear()

    stop_task = asyncio.ensure_future(stop.wait())
    backoff, consecutive_errors = 2, 0
    try:
        while not stop.is_set():
            try:
                async with websockets.connect(ENDPOINT, ping_interval=20,
                                              max_size=2 ** 22) as ws:
                    await ws.send(json.dumps(
                        build_subscription(api_key, boxes, mmsis)))
                    print(f"subscribed: {len(boxes)} box(es), "
                          f"{len(mmsis or [])} mmsi filter(s)", file=sys.stderr)
                    backoff = 2

                    while not stop.is_set():
                        recv = asyncio.ensure_future(ws.recv())
                        # Race the socket against the stop event so Ctrl-C is
                        # immediate rather than waiting out the idle timeout.
                        done, _ = await asyncio.wait(
                            {recv, stop_task}, timeout=IDLE_TIMEOUT,
                            return_when=asyncio.FIRST_COMPLETED)
                        if stop_task in done:
                            recv.cancel()
                            break
                        if recv not in done:
                            recv.cancel()
                            print("\nno data for "
                                  f"{IDLE_TIMEOUT}s, reconnecting", file=sys.stderr)
                            break
                        raw = recv.result()

                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        if "error" in msg:
                            consecutive_errors += 1
                            print(f"server error: {msg['error']}", file=sys.stderr)
                            if consecutive_errors >= 5:
                                print("giving up after 5 consecutive server "
                                      "errors (bad API key?)", file=sys.stderr)
                                stop.set()
                            await asyncio.sleep(5)
                            continue
                        consecutive_errors = 0

                        stats["msgs"] += 1
                        pos, static = parse_message(msg)
                        if pos:
                            buf.append(pos)
                        if static and cache.should_write(static):
                            db.upsert_vessel(
                                conn, static["mmsi"], static["ts"],
                                imo=static.get("imo"), name=static.get("name"),
                                callsign=static.get("callsign"),
                                ship_type=static.get("ship_type"),
                                flag=static.get("flag"),
                                length_m=static.get("length_m"),
                                width_m=static.get("width_m"))
                            stats["static"] += 1
                        if len(buf) >= flush_every:
                            flush()
                            conn.commit()
                            if verbose:
                                print(f"\rmsgs={stats['msgs']:,} "
                                      f"stored={stats['pos']:,} "
                                      f"identities={stats['static']:,}",
                                      end="", file=sys.stderr)
            except asyncio.CancelledError:
                break
            except Exception as exc:    # noqa: BLE001
                if stop.is_set():
                    break
                print(f"\nconnection issue: {exc}; retry in {backoff}s",
                      file=sys.stderr)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=backoff)
                backoff = min(backoff * 2, 120)
    finally:
        stop_task.cancel()
        flush()
        conn.commit()
        conn.close()
    print(f"\nstopped. stored {stats['pos']:,} positions, "
          f"{stats['static']:,} identity records", file=sys.stderr)


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="shadowfleet collect", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=db.DEFAULT_DB)
    ap.add_argument("--api-key", default=os.environ.get("AISSTREAM_API_KEY"))
    ap.add_argument("--region", nargs="*", choices=sorted(zones.REGIONS))
    ap.add_argument("--bbox", nargs=4, type=float,
                    metavar=("LAT1", "LON1", "LAT2", "LON2"))
    ap.add_argument("--zones", action="store_true",
                    help="subscribe to the terminal/STS zone boxes")
    ap.add_argument("--mmsi", nargs="*", help="explicit MMSI filter")
    ap.add_argument("--watchlist", action="store_true",
                    help="filter to MMSIs already linked to a designated IMO")
    ap.add_argument("--flush-every", type=int, default=200)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    if websockets is None:
        sys.exit("live collection needs websockets: "
                 "pip install 'shadow-fleet-toolkit[live]'")
    if not args.api_key:
        sys.exit("set AISSTREAM_API_KEY or pass --api-key (free at aisstream.io)")

    boxes = []
    if args.region:
        boxes += [zones.REGIONS[r] for r in args.region]
    if args.bbox:
        a, b, c, d = args.bbox
        boxes.append([[min(a, c), min(b, d)], [max(a, c), max(b, d)]])
    if args.zones:
        boxes += zones.bounding_boxes(categories={"ru_terminal", "sts"})

    mmsis = list(args.mmsi or [])
    if args.watchlist:
        conn = db.connect(args.db)
        mmsis += db.watchlist_mmsis(conn)
        conn.close()
        if not mmsis:
            print("watchlist empty - run 'shadowfleet sanctions' first, and "
                  "collect static messages so IMOs can be linked to MMSIs",
                  file=sys.stderr)
        elif len(mmsis) > MMSI_FILTER_LIMIT:
            print(f"warning: {len(mmsis)} MMSIs but the API accepts "
                  f"{MMSI_FILTER_LIMIT}; the rest are NOT filtered for. "
                  f"Collect by region instead.", file=sys.stderr)

    try:
        asyncio.run(run(args.api_key, boxes, mmsis, args.db,
                        args.flush_every, not args.quiet))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
