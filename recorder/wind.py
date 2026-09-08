"""Vent 10 m autour de la flotte via Open-Meteo (GFS), sans clé API.

Grille régulière → champ u/v (m/s) pour un calque de particules type Windy / YB.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

OPEN_METEO_GFS = "https://api.open-meteo.com/v1/gfs"
KN_TO_MS = 0.514444


def wind_grid_axes(
    lat: float,
    lon: float,
    *,
    half_lat: float = 5.0,
    half_lon: float = 8.0,
    n_lat: int = 14,
    n_lon: int = 18,
) -> tuple[list[float], list[float]]:
    """Axes de grille : latitudes nord→sud, longitudes ouest→est."""
    n_lat = max(2, n_lat)
    n_lon = max(2, n_lon)
    lats = [
        round(lat + half_lat - (2 * half_lat) * i / (n_lat - 1), 4) for i in range(n_lat)
    ]
    lons = [
        round(lon - half_lon + (2 * half_lon) * j / (n_lon - 1), 4) for j in range(n_lon)
    ]
    return lats, lons


def wind_grid_coords(
    lat: float,
    lon: float,
    *,
    half_lat: float = 5.0,
    half_lon: float = 8.0,
    n_lat: int = 14,
    n_lon: int = 18,
) -> tuple[list[float], list[float]]:
    """Grille régulière aplatie (ligne = parallèle, ouest→est, puis sud)."""
    lats_1d, lons_1d = wind_grid_axes(
        lat, lon, half_lat=half_lat, half_lon=half_lon, n_lat=n_lat, n_lon=n_lon
    )
    lats: list[float] = []
    lons: list[float] = []
    for y in lats_1d:
        for x in lons_1d:
            lats.append(y)
            lons.append(x)
    return lats, lons


def parse_open_meteo(payload: Any) -> dict[str, Any]:
    """Normalise une réponse Open-Meteo (objet unique ou liste multi-points)."""
    rows = payload if isinstance(payload, list) else [payload]
    points: list[dict[str, Any]] = []
    fetched_at: int | None = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        cur = row.get("current") or {}
        spd = cur.get("wind_speed_10m")
        direc = cur.get("wind_direction_10m")
        if spd is None or direc is None:
            continue
        gust = cur.get("wind_gusts_10m")
        points.append(
            {
                "lat": float(row["latitude"]),
                "lon": float(row["longitude"]),
                "speed_kn": round(float(spd), 1),
                "gust_kn": round(float(gust), 1) if gust is not None else None,
                "dir_from": int(round(float(direc))) % 360,
            }
        )
        if fetched_at is None and cur.get("time") is not None:
            try:
                fetched_at = int(cur["time"])
            except (TypeError, ValueError):
                fetched_at = None
    return {
        "source": "open-meteo",
        "model": "GFS",
        "height_m": 10,
        "unit": "kn",
        "at": fetched_at,
        "points": points,
    }


def uv_from_meteo(speed_kn: float, dir_from: float) -> tuple[float, float]:
    """Composantes u (est) / v (nord) en m/s ; dir_from = d’où vient le vent."""
    spd = float(speed_kn) * KN_TO_MS
    rad = math.radians(float(dir_from))
    return -spd * math.sin(rad), -spd * math.cos(rad)


def velocity_grib(
    points: list[dict[str, Any]],
    lats_1d: list[float],
    lons_1d: list[float],
    ref_time: int | None = None,
) -> list[dict[str, Any]]:
    """Champ au format grib2json attendu par leaflet-velocity (scan nord→sud)."""
    ny, nx = len(lats_1d), len(lons_1d)
    n = nx * ny
    u = [0.0] * n
    v = [0.0] * n
    for i, p in enumerate(points[:n]):
        uu, vv = uv_from_meteo(p.get("speed_kn") or 0, p.get("dir_from") or 0)
        u[i] = round(uu, 3)
        v[i] = round(vv, 3)
    la1, la2 = float(lats_1d[0]), float(lats_1d[-1])
    lo1, lo2 = float(lons_1d[0]), float(lons_1d[-1])
    dx = (lo2 - lo1) / (nx - 1) if nx > 1 else 1.0
    dy = abs(la1 - la2) / (ny - 1) if ny > 1 else 1.0
    if ref_time:
        ref = datetime.fromtimestamp(ref_time, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        ref = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    base = {
        "parameterCategory": 2,
        "lo1": lo1,
        "la1": la1,
        "lo2": lo2,
        "la2": la2,
        "nx": nx,
        "ny": ny,
        "dx": round(dx, 5),
        "dy": round(dy, 5),
        "scanMode": 64,
        "refTime": ref,
        "forecastTime": 0,
    }
    return [
        {
            "header": {**base, "parameterNumber": 2, "parameterUnit": "m.s-1"},
            "data": u,
        },
        {
            "header": {**base, "parameterNumber": 3, "parameterUnit": "m.s-1"},
            "data": v,
        },
    ]


def nearest_wind(points: list[dict[str, Any]], lat: float, lon: float) -> dict[str, Any] | None:
    if not points:
        return None
    best = min(points, key=lambda p: (p["lat"] - lat) ** 2 + (p["lon"] - lon) ** 2)
    return {
        "speed_kn": best["speed_kn"],
        "gust_kn": best.get("gust_kn"),
        "dir_from": best["dir_from"],
    }


async def fetch_wind_grid(
    lat: float,
    lon: float,
    client: Any | None = None,
) -> dict[str, Any]:
    """Vent GFS 10 m (nœuds + champ u/v pour particules)."""
    import httpx

    lats_1d, lons_1d = wind_grid_axes(lat, lon)
    lats, lons = wind_grid_coords(lat, lon)
    owns = client is None
    client = client or httpx.AsyncClient(timeout=25.0, headers={"User-Agent": "ggr-vacations/0.1"})
    try:
        resp = await client.get(
            OPEN_METEO_GFS,
            params={
                "latitude": ",".join(str(v) for v in lats),
                "longitude": ",".join(str(v) for v in lons),
                "current": "wind_speed_10m,wind_direction_10m,wind_gusts_10m",
                "wind_speed_unit": "kn",
                "timeformat": "unixtime",
            },
        )
        resp.raise_for_status()
        parsed = parse_open_meteo(resp.json())
        parsed["velocity"] = velocity_grib(parsed["points"], lats_1d, lons_1d, parsed.get("at"))
        parsed["at_centroid"] = nearest_wind(parsed["points"], lat, lon)
        # Le JSON page n’a pas besoin des 250 points : le champ velocity suffit.
        parsed["points"] = []
        return parsed
    finally:
        if owns:
            await client.aclose()
