"""Areas of interest.

Coordinates are approximate centre points with a generous radius in nautical
miles - they are intended for triage ("did this happen near a known STS
anchorage?"), NOT for anything requiring navigational precision. Verify and
tighten them against a real chart before publishing findings.

Anchorage usage shifts constantly. Treat this file as a starting point you
maintain, not as ground truth.
"""
from __future__ import annotations

import math

from .geo import haversine_nm, in_circle

# name -> (lat, lon, radius_nm, category)
ZONES = {
    # --- Russian crude / product export terminals ---------------------------
    "Primorsk":            (60.34,  28.72, 15, "ru_terminal"),
    "Ust-Luga":            (59.66,  28.30, 15, "ru_terminal"),
    "Vysotsk":             (60.61,  28.57, 12, "ru_terminal"),
    "St Petersburg":       (59.87,  30.23, 12, "ru_terminal"),
    "Novorossiysk":        (44.70,  37.79, 20, "ru_terminal"),
    "CPC Yuzhnaya Ozereyevka": (44.61, 37.66, 12, "ru_terminal"),
    "Tuapse":              (44.09,  39.07, 12, "ru_terminal"),
    "Kozmino":             (42.73, 133.02, 15, "ru_terminal"),
    "De-Kastri":           (51.47, 140.79, 15, "ru_terminal"),
    "Prigorodnoye":        (46.63, 142.90, 15, "ru_terminal"),
    "Murmansk / Kola Bay": (69.10,  33.45, 25, "ru_terminal"),
    "Sabetta":             (71.26,  72.06, 20, "ru_terminal"),
    "Portovaya":           (60.60,  28.42, 10, "ru_terminal"),
    "Taman":               (45.20,  36.70, 12, "ru_terminal"),

    # --- Ship-to-ship transfer / lightering hotspots -----------------------
    "Laconian Gulf":       (36.55,  22.75, 25, "sts"),
    "Messenian Gulf":      (36.85,  21.95, 20, "sts"),
    "Kalamata approaches": (36.90,  22.10, 15, "sts"),
    "Ceuta / Gibraltar":   (35.90,  -5.30, 20, "sts"),
    "Augusta Sicily":      (37.20,  15.25, 15, "sts"),
    "Fujairah / Khor Fakkan": (25.30, 56.55, 30, "sts"),
    "Gulf of Oman":        (24.80,  58.50, 60, "sts"),
    "Riau / Karimun":      ( 1.05, 103.50, 35, "sts"),
    "Tanjung Pelepas":     ( 1.30, 103.55, 20, "sts"),
    "Yeosu approaches":    (34.70, 127.75, 20, "sts"),
    "Nakhodka Bay":        (42.80, 132.90, 20, "sts"),
    "Skagen / Kattegat":   (57.72,  10.90, 20, "sts"),
    "Gulf of Finland E":   (60.00,  27.00, 30, "sts"),
    "Kaliningrad approach":(54.90,  19.60, 25, "sts"),
    "Kalymnos / Aegean":   (36.95,  27.05, 20, "sts"),
    "Suez Gulf anchorage": (29.35,  32.60, 20, "sts"),
    "Sohar / Oman coast":  (24.50,  56.75, 20, "sts"),

    # --- Chokepoints worth monitoring --------------------------------------
    "Danish Straits":      (55.60,  11.10, 35, "chokepoint"),
    "Oresund":             (55.85,  12.75, 15, "chokepoint"),
    "Gulf of Finland W":   (59.80,  25.00, 40, "chokepoint"),
    "Bosphorus":           (41.12,  29.06, 12, "chokepoint"),
    "Dardanelles":         (40.20,  26.40, 15, "chokepoint"),
    "Strait of Gibraltar": (35.95,  -5.60, 20, "chokepoint"),
    "Dover Strait":        (50.95,   1.50, 20, "chokepoint"),
    "Suez Canal":          (30.50,  32.35, 30, "chokepoint"),
    "Malacca Strait":      ( 2.50, 101.50, 60, "chokepoint"),
    "Bab el-Mandeb":       (12.60,  43.40, 30, "chokepoint"),
    "La Perouse Strait":   (45.75, 142.00, 30, "chokepoint"),
}


def zone_for(lat: float, lon: float, categories=None):
    """Return the name of the closest matching zone containing the point."""
    best, best_d = None, None
    for name, (zlat, zlon, radius, cat) in ZONES.items():
        if categories and cat not in categories:
            continue
        if not in_circle(lat, lon, zlat, zlon, radius):
            continue
        d = haversine_nm(lat, lon, zlat, zlon)
        if best_d is None or d < best_d:
            best, best_d = name, d
    return best


def category_of(name: str):
    z = ZONES.get(name)
    return z[3] if z else None


def bounding_boxes(categories=None):
    """Coarse bounding boxes for AIS subscriptions, as [[lat1,lon1],[lat2,lon2]]."""
    boxes = []
    for _name, (lat, lon, radius, cat) in ZONES.items():
        if categories and cat not in categories:
            continue
        dlat = radius / 60.0
        dlon = radius / 60.0 / max(0.1, abs(math.cos(math.radians(lat))))
        boxes.append([[lat - dlat, lon - dlon], [lat + dlat, lon + dlon]])
    return boxes


# Wide regional boxes, handy for `ais_collect.py --region`
REGIONS = {
    "baltic":     [[53.5, 9.0], [66.0, 30.5]],
    "north_sea":  [[51.0, -4.0], [61.0, 9.0]],
    "black_sea":  [[40.5, 27.0], [47.5, 42.0]],
    "med_east":   [[31.0, 20.0], [41.5, 36.5]],
    "med_west":   [[35.0, -6.5], [44.0, 20.0]],
    "arctic_ru":  [[66.0, 15.0], [78.0, 90.0]],
    "gulf_oman":  [[21.0, 54.0], [27.5, 62.0]],
    "malacca":    [[-2.0, 98.0], [7.0, 106.0]],
    "far_east_ru":[[40.0, 128.0], [55.0, 145.0]],
    "world":      [[-90.0, -180.0], [90.0, 180.0]],
}
