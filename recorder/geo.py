"""Géodésie simple (haversine, centroïde) — pas de dépendance numpy."""

from __future__ import annotations

import math

EARTH_KM = 6371.0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance orthodromique en kilomètres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_KM * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1 - a)))


def centroid(points: list[tuple[float, float]]) -> tuple[float, float] | None:
    """Centroïde sphérique approximé (moyenne vectorielle unitaire)."""
    if not points:
        return None
    x = y = z = 0.0
    for lat, lon in points:
        p = math.radians(lat)
        l = math.radians(lon)
        x += math.cos(p) * math.cos(l)
        y += math.cos(p) * math.sin(l)
        z += math.sin(p)
    n = len(points)
    x, y, z = x / n, y / n, z / n
    hyp = math.sqrt(x * x + y * y)
    lon = math.degrees(math.atan2(y, x))
    lat = math.degrees(math.atan2(z, hyp))
    return lat, lon


def fmt_latlon(lat: float, lon: float) -> str:
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    return f"{abs(lat):.3f}°{ns} {abs(lon):.3f}°{ew}"
