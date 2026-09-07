"""Sélection des KiwiSDR les plus adaptés à la position de la flotte."""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import urlparse

from recorder.geo import fmt_latlon, haversine_km

log = logging.getLogger(__name__)

_ARRAY_RE = re.compile(r"=\s*(\[[\s\S]*\])\s*;?\s*$")


def parse_kiwi_directory(raw: str) -> list[dict[str, Any]]:
    """Parse le JS rx.linkfanel.net/kiwisdr_com.js (JSON presque valide)."""
    match = _ARRAY_RE.search(raw)
    blob = match.group(1) if match else raw
    blob = re.sub(r",\s*([\]}])", r"\1", blob)
    data = json.loads(blob)
    if not isinstance(data, list):
        return []
    return [row for row in data if isinstance(row, dict)]


def _parse_gps(raw: str) -> tuple[float, float] | None:
    match = re.search(r"\(\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\)", raw or "")
    if not match:
        return None
    return float(match.group(1)), float(match.group(2))


def _parse_bands(raw: str) -> tuple[int, int] | None:
    match = re.search(r"(-?\d+)\s*-\s*(-?\d+)", raw or "")
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _snr_hf(raw: str) -> float:
    # Champ « snr » Kiwi : "HF,MF" approximatif, premier nombre = HF
    try:
        return float(str(raw).split(",")[0])
    except (TypeError, ValueError):
        return 0.0


def _covers_hf(bands: tuple[int, int] | None, min_hz: int, max_hz: int) -> bool:
    if not bands:
        return False
    lo, hi = bands
    return lo <= min_hz and hi >= max_hz


def _host_port(url: str) -> tuple[str, int, bool] | None:
    parsed = urlparse(url if "://" in url else f"http://{url}")
    if not parsed.hostname:
        return None
    https = parsed.scheme == "https"
    port = parsed.port or (443 if https else 8073)
    return parsed.hostname, port, https


def score_kiwi(
    kiwi: dict[str, Any],
    fleet_lat: float,
    fleet_lon: float,
) -> float:
    """Score 0–1 : proximité flotte, SNR HF, places libres."""
    dist = float(kiwi.get("distance_km") or 0)
    snr = min(max(float(kiwi.get("snr_hf") or 0) / 40.0, 0.0), 1.0)
    free = min(max(float(kiwi.get("free_slots") or 0) / 4.0, 0.0), 1.0)
    near = 1.0 / (1.0 + dist / 2000.0)
    return 0.45 * near + 0.40 * snr + 0.15 * free


def normalize_receiver(row: dict[str, Any], fleet_lat: float, fleet_lon: float, cfg: dict[str, Any]) -> dict[str, Any] | None:
    if str(row.get("offline") or "").lower() not in ("no", "0", ""):
        return None
    if str(row.get("status") or "active").lower() not in ("active", ""):
        return None
    gps = _parse_gps(str(row.get("gps") or ""))
    if not gps:
        return None
    bands = _parse_bands(str(row.get("bands") or ""))
    sdr_cfg = cfg.get("sdr") or {}
    min_hz = int(sdr_cfg.get("min_freq_hz") or 12_000_000)
    max_hz = int(sdr_cfg.get("max_freq_hz") or 17_000_000)
    if not _covers_hf(bands, min_hz, max_hz):
        return None
    url = str(row.get("url") or "").strip()
    hp = _host_port(url)
    if not hp:
        return None
    host, port, https = hp
    try:
        users = int(row.get("users") or 0)
        users_max = int(row.get("users_max") or 0)
    except (TypeError, ValueError):
        return None
    free = max(0, users_max - users)
    min_free = int(sdr_cfg.get("min_free_slots") or 1)
    if free < min_free:
        return None
    if str(row.get("ant_connected") or "1") in ("0", "no", "false"):
        return None
    dist = haversine_km(fleet_lat, fleet_lon, gps[0], gps[1])
    kiwi = {
        "id": row.get("id"),
        "name": row.get("name") or host,
        "url": url.rstrip("/"),
        "host": host,
        "port": port,
        "https": https,
        "lat": gps[0],
        "lon": gps[1],
        "locator": row.get("grid"),
        "loc": row.get("loc"),
        "antenna": row.get("antenna"),
        "snr_hf": _snr_hf(str(row.get("snr") or "")),
        "users": users,
        "users_max": users_max,
        "free_slots": free,
        "distance_km": round(dist, 1),
        "fmt": fmt_latlon(gps[0], gps[1]),
    }
    kiwi["score"] = round(score_kiwi(kiwi, fleet_lat, fleet_lon), 4)
    return kiwi


async def fetch_ranked_kiwis(
    cfg: dict[str, Any],
    fleet_lat: float,
    fleet_lon: float,
    client: Any | None = None,
    limit: int = 12,
) -> list[dict[str, Any]]:
    import httpx

    sdr_cfg = cfg.get("sdr") or {}
    url = sdr_cfg.get("directory_url") or "http://rx.linkfanel.net/kiwisdr_com.js"
    owns = client is None
    client = client or httpx.AsyncClient(timeout=40.0, headers={"User-Agent": "ggr-vacations/0.1"})
    try:
        resp = await client.get(url)
        resp.raise_for_status()
        rows = parse_kiwi_directory(resp.text)
    finally:
        if owns:
            await client.aclose()
    ranked: list[dict[str, Any]] = []
    for row in rows:
        kiwi = normalize_receiver(row, fleet_lat, fleet_lon, cfg)
        if kiwi:
            ranked.append(kiwi)
    ranked.sort(key=lambda k: k["score"], reverse=True)
    log.info("KiwiSDR : %s récepteurs classés (flotte %.3f, %.3f)", len(ranked), fleet_lat, fleet_lon)
    return ranked[:limit]


def kiwi_tune_url(kiwi: dict[str, Any], freq_khz: float, mode: str = "usb", zoom: int = 10) -> str:
    """URL KiwiSDR pré-accordée (QRG kHz + mode USB + zoom waterfall)."""
    base = kiwi["url"].rstrip("/")
    return f"{base}/?f={freq_khz:.2f}{mode}z{int(zoom)}"
