"""Chasse de signaux USB sur le waterfall KiwiSDR (flux WS WF)."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any
from urllib.parse import quote

import websockets
from websockets.exceptions import ConnectionClosed

from recorder.kiwi_audio import _parse_msg, _ws_uris

log = logging.getLogger(__name__)

WF_BINS = 1024
MAX_FREQ_KHZ = 30_000.0
HUNT_LO_KHZ = 14_150.0
HUNT_HI_KHZ = 14_260.0
HUNT_ZOOM = 8
# Zoom 8 → 30 000 / 256 = 117,1875 kHz, centré pour couvrir 14 150–14 260.
HUNT_CF_KHZ = 14_205.0
HUNT_LINES = 12


def span_khz(zoom: int, max_freq_khz: float = MAX_FREQ_KHZ) -> float:
    return max_freq_khz / (2 ** int(zoom))


def bin_freq_khz(
    index: int,
    zoom: int,
    cf_khz: float,
    *,
    max_freq_khz: float = MAX_FREQ_KHZ,
) -> float:
    span = span_khz(zoom, max_freq_khz)
    start = cf_khz - span / 2.0
    return start + (index + 0.5) * span / WF_BINS


def usb_dial_from_spectrum(
    bins: list[float],
    *,
    zoom: int,
    cf_khz: float,
    lo_khz: float = HUNT_LO_KHZ,
    hi_khz: float = HUNT_HI_KHZ,
    max_freq_khz: float = MAX_FREQ_KHZ,
) -> dict[str, Any] | None:
    """Accord USB = bord gauche du blob le plus fort (téléphonie 20 m)."""
    if len(bins) < WF_BINS:
        return None
    freqs = [bin_freq_khz(i, zoom, cf_khz, max_freq_khz=max_freq_khz) for i in range(WF_BINS)]
    # Écarter les 2 kHz aux bords de fenêtre (artefacts CIC / blob tronqué).
    mask = [lo_khz + 2.0 <= f <= hi_khz - 2.0 for f in freqs]
    band = [bins[i] for i, ok in enumerate(mask) if ok]
    if len(band) < 16:
        return None
    ordered = sorted(band)
    noise = ordered[max(0, len(ordered) // 5)]
    peak = ordered[-1]
    if peak - noise < 18:
        return None
    thr = noise + 0.35 * (peak - noise)
    bin_hz = span_khz(zoom, max_freq_khz) / WF_BINS * 1000.0
    min_w = max(6, int(1.2e3 / bin_hz))
    max_w = max(min_w + 1, int(5.0e3 / bin_hz))

    best: tuple[float, int, int] | None = None
    i = 0
    while i < WF_BINS:
        if not mask[i] or bins[i] < thr:
            i += 1
            continue
        j = i
        while j < WF_BINS and mask[j] and bins[j] >= thr:
            j += 1
        width = j - i
        if min_w <= width <= max_w:
            prominence = max(bins[i:j]) - noise
            if best is None or prominence > best[0]:
                best = (prominence, i, j)
        i = j
    if best is None:
        inner = [i for i, ok in enumerate(mask) if ok]
        if not inner:
            return None
        idx = max(inner, key=lambda k: bins[k])
        if bins[idx] - noise < 18:
            return None
        dial = freqs[idx] - 1.4
    else:
        dial = freqs[best[1]]
    dial = min(max(dial, lo_khz), hi_khz - 0.5)
    dial = round(dial * 20.0) / 20.0
    return {
        "freq_khz": dial,
        "peak_db": round(peak, 1),
        "noise": round(noise, 1),
        "cf_khz": cf_khz,
        "zoom": zoom,
    }


def _parse_wf_line(message: bytes) -> list[int] | None:
    """Paquet W/F non compressé (kiwiclient : tag + 1 octet + 3×u32 LE + 1024 octets)."""
    if len(message) < 3 + 1 + 12 + WF_BINS or message[:3] != b"W/F":
        return None
    data = message[16:]
    if len(data) < WF_BINS:
        return None
    return list(data[:WF_BINS])


async def hunt_usb_signal(
    kiwi: dict[str, Any],
    *,
    lo_khz: float = HUNT_LO_KHZ,
    hi_khz: float = HUNT_HI_KHZ,
    ident: str = "ggr-vacations",
    timeout_s: float = 12.0,
) -> dict[str, Any] | None:
    """Ouvre le WF, moyenne quelques lignes, renvoie l’accord USB ou None."""
    origin = kiwi.get("url") or f"http://{kiwi['host']}:{kiwi['port']}"
    last_err = None
    for uri in _ws_uris(kiwi, "WF"):
        try:
            found = await _hunt_on_uri(
                uri,
                origin,
                kiwi,
                lo_khz=lo_khz,
                hi_khz=hi_khz,
                ident=ident,
                timeout_s=timeout_s,
            )
            if found:
                return found
            last_err = "aucun blob USB dans la fenêtre"
        except Exception as exc:
            last_err = str(exc)
            log.warning("WF Kiwi %s %s : %s", kiwi.get("name"), uri, exc)
    if last_err:
        log.info("Chasse WF %s : %s", kiwi.get("name"), last_err)
    return None


async def _hunt_on_uri(
    uri: str,
    origin: str,
    kiwi: dict[str, Any],
    *,
    lo_khz: float,
    hi_khz: float,
    ident: str,
    timeout_s: float,
) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout_s
    lines: list[list[int]] = []
    setup_done = False
    max_freq = MAX_FREQ_KHZ
    opened = time.monotonic()
    msg_n = 0

    async with websockets.connect(
        uri,
        max_size=2**22,
        open_timeout=15,
        close_timeout=5,
        compression=None,
        ping_interval=None,
        origin=origin,
        user_agent_header="ggr-vacations/0.1.8",
    ) as ws:
        await ws.send("SET auth t=kiwi p=")
        last_keep = 0.0
        while time.monotonic() < deadline and len(lines) < HUNT_LINES:
            try:
                message = await asyncio.wait_for(ws.recv(), timeout=4)
            except TimeoutError:
                if msg_n == 0 and time.monotonic() - opened >= 8:
                    return None
                await ws.send("SET keepalive")
                continue
            except ConnectionClosed:
                break
            now = time.monotonic()
            if now - last_keep >= 1.0:
                await ws.send("SET keepalive")
                last_keep = now

            if isinstance(message, str) or (
                isinstance(message, bytes) and message[:3] == b"MSG"
            ):
                params = _parse_msg(message)
                msg_n += 1
                if "too_busy" in params:
                    raise RuntimeError(f"Kiwi occupé ({kiwi.get('name')})")
                if params.get("badp") == "1":
                    raise RuntimeError(f"Mot de passe Kiwi requis ({kiwi.get('name')})")
                if params.get("bandwidth"):
                    try:
                        max_freq = float(params["bandwidth"]) / 1000.0
                    except ValueError:
                        pass
                if not setup_done and (
                    "wf_setup" in params or params.get("auth") == "kiwi"
                ):
                    await ws.send(f"SET ident_user={quote(ident, safe='')}")
                    await ws.send("SET geo=ggr-vacations")
                    await ws.send(f"SET zoom={HUNT_ZOOM} cf={HUNT_CF_KHZ:.3f}")
                    await ws.send("SET maxdb=-10 mindb=-110")
                    await ws.send("SET wf_comp=0")
                    await ws.send("SET wf_speed=4")
                    await ws.send("SET interp=13")
                    setup_done = True
                    log.info("WF prêt %s zoom %s cf %.2f", kiwi.get("name"), HUNT_ZOOM, HUNT_CF_KHZ)
                continue

            if not isinstance(message, bytes) or message[:3] != b"W/F":
                continue
            row = _parse_wf_line(message)
            if row:
                lines.append(row)

    if len(lines) < 3:
        return None
    acc = [0.0] * WF_BINS
    for row in lines:
        for i, v in enumerate(row):
            acc[i] += v
    avg = [v / len(lines) for v in acc]
    hit = usb_dial_from_spectrum(
        avg,
        zoom=HUNT_ZOOM,
        cf_khz=HUNT_CF_KHZ,
        lo_khz=lo_khz,
        hi_khz=hi_khz,
        max_freq_khz=max_freq,
    )
    if hit:
        hit["kiwi"] = kiwi.get("name")
        hit["lines"] = len(lines)
        log.info(
            "Trafic USB %.2f kHz (pic %.0f, bruit %.0f, %s lignes) sur %s",
            hit["freq_khz"],
            hit["peak_db"],
            hit["noise"],
            len(lines),
            kiwi.get("name"),
        )
    return hit
