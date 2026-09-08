"""Vent 10 m autour de la flotte via Open-Meteo (GFS), nœuds, sans clé API."""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

OPEN_METEO_GFS = "https://api.open-meteo.com/v1/gfs"


def wind_grid_coords(
    lat: float,
    lon: float,
    *,
    half_lat: float = 3.5,
    half_lon: float = 5.0,
    n_lat: int = 5,
    n_lon: int = 6,
) -> tuple[list[float], list[float]]:
    """Grille régulière centrée sur (lat, lon)."""
    lats: list[float] = []
    lons: list[float] = []
    n_lat = max(1, n_lat)
    n_lon = max(1, n_lon)
    for i in range(n_lat):
        y = lat if n_lat == 1 else lat - half_lat + (2 * half_lat) * i / (n_lat - 1)
        for j in range(n_lon):
            x = lon if n_lon == 1 else lon - half_lon + (2 * half_lon) * j / (n_lon - 1)
            lats.append(round(y, 4))
            lons.append(round(x, 4))
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


async def fetch_wind_grid(
    lat: float,
    lon: float,
    client: Any | None = None,
) -> dict[str, Any]:
    """Vent GFS 10 m (nœuds, direction météo = d’où il vient)."""
    import httpx

    lats, lons = wind_grid_coords(lat, lon)
    owns = client is None
    client = client or httpx.AsyncClient(timeout=20.0, headers={"User-Agent": "ggr-vacations/0.1"})
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
        return parse_open_meteo(resp.json())
    finally:
        if owns:
            await client.aclose()
