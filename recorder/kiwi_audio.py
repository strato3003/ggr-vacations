"""Client audio KiwiSDR (WebSocket SND, PCM 12 kHz non compressé)."""

from __future__ import annotations

import asyncio
import logging
import struct
import time
import wave
from pathlib import Path
from typing import Any
from urllib.parse import quote

import websockets
from websockets.exceptions import ConnectionClosed

log = logging.getLogger(__name__)

SAMPLE_RATE = 12_000


def _ws_uri(kiwi: dict[str, Any]) -> str:
    scheme = "wss" if kiwi.get("https") else "ws"
    ts = int(time.time())
    return f"{scheme}://{kiwi['host']}:{kiwi['port']}/{ts}/SND"


def _parse_msg(payload: bytes | str) -> dict[str, str]:
    if isinstance(payload, bytes):
        if payload[:3] == b"MSG":
            text = payload[4:].decode("utf-8", "replace")
        else:
            text = payload.decode("utf-8", "replace")
    else:
        text = payload
        if text.startswith("MSG"):
            text = text[3:].lstrip()
    params: dict[str, str] = {}
    for part in text.split():
        if "=" in part:
            key, value = part.split("=", 1)
            params[key] = value
        else:
            params[part] = ""
    return params


def _pcm_from_snd(body: bytes) -> bytes:
    """Extrait le PCM s16be du corps SND et le convertit en s16le pour WAV."""
    if len(body) < 8:
        return b""
    flags = body[0]
    data = body[7:]
    if flags & 0x10:
        # Compressé IMA-ADPCM : on exige compression=0
        return b""
    if flags & 0x08:
        data = data[10:]  # en-tête GPS stéréo
    if len(data) < 2:
        return b""
    count = len(data) // 2
    if flags & 0x80:
        return data[: count * 2]
    samples = struct.unpack(">" + "h" * count, data[: count * 2])
    return struct.pack("<" + "h" * count, *samples)


async def record_kiwi_wav(
    kiwi: dict[str, Any],
    freq_khz: float,
    dest: Path,
    duration_s: float,
    *,
    mode: str = "usb",
    low_hz: int = 300,
    high_hz: int = 2700,
    ident: str = "ggr-vacations",
    on_rssi: Any | None = None,
    freq_plan: list[tuple[float, float]] | None = None,
) -> dict[str, Any]:
    """Enregistre un WAV mono 12 kHz depuis un KiwiSDR.

    Protocole public KiwiSDR : GET /{timestamp}/SND puis commandes SET.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    uri = _ws_uri(kiwi)
    plan = freq_plan or [(freq_khz, duration_s)]
    current_freq = plan[0][0]
    info: dict[str, Any] = {
        "kiwi": kiwi.get("name"),
        "url": kiwi.get("url"),
        "freq_khz": current_freq,
        "mode": mode,
        "path": str(dest),
        "ok": False,
        "scan": [{"freq_khz": f, "dwell_s": d} for f, d in plan],
    }
    deadline = time.monotonic() + sum(d for _, d in plan)
    pcm_chunks: list[bytes] = []
    frames = 0
    last_keep = 0.0
    last_rssi: float | None = None
    step_i = 0
    step_until = time.monotonic() + plan[0][1]

    try:
        async with websockets.connect(
            uri,
            max_size=2**22,
            open_timeout=20,
            close_timeout=5,
            additional_headers={
                "User-Agent": "ggr-vacations/0.1",
                "Origin": kiwi.get("url") or f"http://{kiwi['host']}",
            },
        ) as ws:
            await ws.send("SET auth t=kiwi p=")
            while time.monotonic() < deadline:
                try:
                    message = await asyncio.wait_for(ws.recv(), timeout=5)
                except TimeoutError:
                    now = time.monotonic()
                    await ws.send("SET keepalive")
                    last_keep = now
                    if step_i + 1 < len(plan) and now >= step_until:
                        step_i += 1
                        current_freq, dwell = plan[step_i]
                        step_until = now + dwell
                        await ws.send(
                            f"SET mod={mode} low_cut={low_hz} high_cut={high_hz} freq={current_freq:.3f}"
                        )
                        log.info("Audio QSY %.3f kHz USB", current_freq)
                    continue
                except ConnectionClosed:
                    break

                now = time.monotonic()
                if now - last_keep >= 1.0:
                    await ws.send("SET keepalive")
                    last_keep = now
                if step_i + 1 < len(plan) and now >= step_until:
                    step_i += 1
                    current_freq, dwell = plan[step_i]
                    step_until = now + dwell
                    await ws.send(
                        f"SET mod={mode} low_cut={low_hz} high_cut={high_hz} freq={current_freq:.3f}"
                    )
                    log.info("Audio QSY %.3f kHz USB", current_freq)

                if isinstance(message, str) or (isinstance(message, bytes) and message[:3] == b"MSG"):
                    params = _parse_msg(message)
                    if "too_busy" in params:
                        raise RuntimeError(f"Kiwi occupé ({kiwi.get('name')})")
                    if "badp" in params and params.get("badp") == "1":
                        raise RuntimeError(f"Mot de passe Kiwi requis ({kiwi.get('name')})")
                    if "rx_chans" in params or "load_auth" in params or params.get("auth") == "kiwi":
                        await ws.send(f"SET ident_user={quote(ident, safe='')}")
                        await ws.send("SET geo=ggr-vacations")
                        await ws.send("SET compression=0")
                        await ws.send(
                            f"SET mod={mode} low_cut={low_hz} high_cut={high_hz} freq={current_freq:.3f}"
                        )
                        await ws.send("SET agc=1 hang=0 thresh=-100 slope=6 decay=1000 manGain=50")
                        await ws.send("SET AR OK in=12000 out=12000")
                        await ws.send("SET squelch=0 max=0")
                    continue

                if not isinstance(message, bytes) or message[:3] != b"SND":
                    continue
                body = message[3:]
                if len(body) >= 7:
                    smeter = struct.unpack(">H", body[5:7])[0]
                    last_rssi = 0.1 * smeter - 127
                    if on_rssi:
                        on_rssi(last_rssi)
                chunk = _pcm_from_snd(body)
                if chunk:
                    pcm_chunks.append(chunk)
                    frames += len(chunk) // 2
    except Exception as exc:
        info["error"] = str(exc)
        log.warning("Audio Kiwi %s @ %.3f kHz : %s", kiwi.get("name"), current_freq, exc)
        if not pcm_chunks:
            return info

    if not pcm_chunks:
        info["error"] = "aucun échantillon reçu"
        return info

    with wave.open(str(dest), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(b"".join(pcm_chunks))
    info.update(
        {
            "ok": True,
            "seconds": round(frames / SAMPLE_RATE, 1),
            "rssi_dbm": round(last_rssi, 1) if last_rssi is not None else None,
        }
    )
    log.info("WAV %s (%.1f s, RSSI %s)", dest.name, info["seconds"], info["rssi_dbm"])
    return info
