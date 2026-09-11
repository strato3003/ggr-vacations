"""Client audio KiwiSDR (WebSocket SND, PCM 12 kHz).

Firmware ≥ 1.6xx : URI `/ws/kiwi/{ts}/SND`. L’ancien `/{ts}/SND` (kiwiclient)
obtient un 101 WebSocket mais n’attache pas le flux — aucun MSG / SND.
Réf. Beagle_SDR_GPS web/kiwi/kiwi_util.js `open_websocket`.
"""

from __future__ import annotations

import array
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
SND_FLAG_STEREO = 0x08
SND_FLAG_COMPRESSED = 0x10
SND_FLAG_LITTLE_ENDIAN = 0x80

# Tables IMA-ADPCM (même valeurs que kiwiclient / kiwi.js).
_STEP = (
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34,
    37, 41, 45, 50, 55, 60, 66, 73, 80, 88, 97, 107, 118, 130, 143,
    157, 173, 190, 209, 230, 253, 279, 307, 337, 371, 408, 449, 494,
    544, 598, 658, 724, 796, 876, 963, 1060, 1166, 1282, 1411, 1552,
    1707, 1878, 2066, 2272, 2499, 2749, 3024, 3327, 3660, 4026,
    4428, 4871, 5358, 5894, 6484, 7132, 7845, 8630, 9493, 10442,
    11487, 12635, 13899, 15289, 16818, 18500, 20350, 22385, 24623,
    27086, 29794, 32767,
)
_INDEX = [-1, -1, -1, -1, 2, 4, 6, 8, -1, -1, -1, -1, 2, 4, 6, 8]


class ImaAdpcmDecoder:
    """Décodeur IMA-ADPCM KiwiSDR (2 échantillons par octet)."""

    def __init__(self) -> None:
        self.index = 0
        self.prev = 0

    def _sample(self, code: int) -> int:
        step = _STEP[self.index]
        self.index = min(max(self.index + _INDEX[code], 0), len(_STEP) - 1)
        diff = step >> 3
        if code & 1:
            diff += step >> 2
        if code & 2:
            diff += step >> 1
        if code & 4:
            diff += step
        if code & 8:
            diff = -diff
        self.prev = min(max(self.prev + diff, -32768), 32767)
        return self.prev

    def decode(self, data: bytes) -> bytes:
        samples = array.array("h")
        for byte in data:
            samples.append(self._sample(byte & 0x0F))
            samples.append(self._sample(byte >> 4))
        return samples.tobytes()


def _ws_uris(kiwi: dict[str, Any], stream: str = "SND") -> list[str]:
    """Chemins WS : firmware récent d’abord, puis kiwiclient historique."""
    scheme = "wss" if kiwi.get("https") else "ws"
    host = kiwi["host"]
    port = int(kiwi["port"])
    ms = int(time.time() * 1000)
    sec = int(time.time())
    origin = f"{scheme}://{host}:{port}"
    return [
        f"{origin}/ws/kiwi/{ms}/{stream}",
        f"{origin}/{sec}/{stream}",
    ]


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


def _pcm_from_snd(body: bytes, decoder: ImaAdpcmDecoder) -> bytes:
    """PCM s16le depuis un paquet SND (corps après le tag 3 octets)."""
    if len(body) < 8:
        return b""
    flags = body[0]
    data = body[7:]
    if flags & SND_FLAG_STEREO:
        if len(data) < 10:
            return b""
        data = data[10:]
    if not data:
        return b""
    if flags & SND_FLAG_COMPRESSED:
        return decoder.decode(data)
    count = len(data) // 2
    raw = data[: count * 2]
    if flags & SND_FLAG_LITTLE_ENDIAN:
        return raw
    samples = struct.unpack(">" + "h" * count, raw)
    return struct.pack("<" + "h" * count, *samples)


def amplify_pcm(pcm: bytes, *, target_peak: int = 28000, max_gain: float = 10.0) -> bytes:
    """Remonte le niveau vers ~ −1 dBFS (le slider Kiwi n’est pas dans le flux SND)."""
    n = len(pcm) // 2
    if n < 8:
        return pcm
    samples = array.array("h")
    samples.frombytes(pcm[: n * 2])
    peak = 0
    for sample in samples:
        a = sample if sample >= 0 else -sample
        if a > peak:
            peak = a
    if peak < 48 or peak >= target_peak:
        return pcm
    gain = min(float(target_peak) / float(peak), float(max_gain))
    out = array.array("h")
    for sample in samples:
        v = int(sample * gain)
        if v > 32767:
            v = 32767
        elif v < -32768:
            v = -32768
        out.append(v)
    log.info("Gain audio ×%.2f (crête %s → %s)", gain, peak, min(32767, int(peak * gain)))
    return out.tobytes()


def wav_from_snd_frames(frames: list[bytes], dest: Path) -> bool:
    """Écrit un WAV 12 kHz à partir de paquets WebSocket bruts (tag SND inclus)."""
    decoder = ImaAdpcmDecoder()
    pcm: list[bytes] = []
    total = 0
    for raw in frames:
        if not isinstance(raw, (bytes, bytearray)) or len(raw) < 10:
            continue
        if raw[:3] != b"SND":
            continue
        chunk = _pcm_from_snd(raw[3:], decoder)
        if chunk:
            pcm.append(chunk)
            total += len(chunk) // 2
    if not pcm:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(dest), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(amplify_pcm(b"".join(pcm)))
    log.info("WAV %s (%.1f s, %s paquets)", dest.name, total / SAMPLE_RATE, len(frames))
    return True


async def _send_rx_setup(
    ws: Any,
    *,
    mode: str,
    low_hz: int,
    high_hz: int,
    freq_khz: float,
    ident: str,
    ar_in: int = 12000,
) -> None:
    await ws.send(f"SET ident_user={quote(ident, safe='')}")
    await ws.send("SET geo=ggr-vacations")
    await ws.send("SET compression=0")
    await ws.send(f"SET mod={mode} low_cut={low_hz} high_cut={high_hz} freq={freq_khz:.3f}")
    await ws.send("SET agc=1 hang=0 thresh=-20 slope=6 decay=1000 manGain=50")
    await ws.send(f"SET AR OK in={ar_in} out=12000")
    await ws.send("SET squelch=0 max=0")


def _needs_rx_setup(params: dict[str, str]) -> bool:
    return any(
        key in params
        for key in ("sample_rate", "audio_rate", "rx_chans", "load_auth")
    ) or params.get("auth") == "kiwi"


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
    """Enregistre un WAV mono 12 kHz depuis un KiwiSDR."""
    dest.parent.mkdir(parents=True, exist_ok=True)
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
    origin = kiwi.get("url") or f"http://{kiwi['host']}:{kiwi['port']}"
    last_error: str | None = None
    for uri in _ws_uris(kiwi):
        info["ws_uri"] = uri
        try:
            result = await _record_on_uri(
                uri,
                origin,
                kiwi,
                dest,
                plan,
                current_freq,
                mode,
                low_hz,
                high_hz,
                ident,
                on_rssi,
                info,
            )
            if result.get("ok") or result.get("frames"):
                return result
            last_error = result.get("error") or last_error
        except Exception as exc:
            last_error = str(exc)
            log.warning("Audio Kiwi %s %s : %s", kiwi.get("name"), uri, exc)
    info["error"] = last_error or "aucun échantillon reçu"
    return info


async def _record_on_uri(
    uri: str,
    origin: str,
    kiwi: dict[str, Any],
    dest: Path,
    plan: list[tuple[float, float]],
    current_freq: float,
    mode: str,
    low_hz: int,
    high_hz: int,
    ident: str,
    on_rssi: Any | None,
    info: dict[str, Any],
) -> dict[str, Any]:
    deadline = time.monotonic() + sum(d for _, d in plan)
    pcm_chunks: list[bytes] = []
    frames = 0
    last_keep = 0.0
    last_rssi: float | None = None
    step_i = 0
    step_until = time.monotonic() + plan[0][1]
    setup_done = False
    decoder = ImaAdpcmDecoder()
    snd_n = 0
    msg_n = 0
    ar_in = 12000
    opened = time.monotonic()

    async with websockets.connect(
        uri,
        max_size=2**22,
        open_timeout=20,
        close_timeout=5,
        compression=None,
        ping_interval=None,
        origin=origin,
        user_agent_header="ggr-vacations/0.1.9",
    ) as ws:
        await ws.send("SET auth t=kiwi p=")
        while time.monotonic() < deadline:
            try:
                message = await asyncio.wait_for(ws.recv(), timeout=5)
            except TimeoutError:
                now = time.monotonic()
                if msg_n == 0 and snd_n == 0 and now - opened >= 8:
                    info["error"] = "pas de MSG sur ce chemin SND"
                    return info
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

            if isinstance(message, str) or (
                isinstance(message, bytes) and message[:3] == b"MSG"
            ):
                params = _parse_msg(message)
                if "too_busy" in params:
                    raise RuntimeError(f"Kiwi occupé ({kiwi.get('name')})")
                if params.get("badp") == "1":
                    raise RuntimeError(f"Mot de passe Kiwi requis ({kiwi.get('name')})")
                if params.get("audio_rate"):
                    try:
                        ar_in = int(float(params["audio_rate"]))
                    except ValueError:
                        pass
                msg_n += 1
                if not setup_done and _needs_rx_setup(params):
                    await _send_rx_setup(
                        ws,
                        mode=mode,
                        low_hz=low_hz,
                        high_hz=high_hz,
                        freq_khz=current_freq,
                        ident=ident,
                        ar_in=ar_in,
                    )
                    setup_done = True
                    log.info("SND prêt %s @ %.3f kHz", uri, current_freq)
                continue

            if not isinstance(message, bytes) or message[:3] != b"SND":
                continue
            snd_n += 1
            body = message[3:]
            if len(body) >= 7:
                smeter = struct.unpack(">H", body[5:7])[0]
                last_rssi = 0.1 * smeter - 127
                if on_rssi:
                    on_rssi(last_rssi)
            chunk = _pcm_from_snd(body, decoder)
            if chunk:
                pcm_chunks.append(chunk)
                frames += len(chunk) // 2

    info["snd_packets"] = snd_n
    info["frames"] = frames
    if not pcm_chunks:
        info["error"] = "aucun échantillon reçu"
        return info

    with wave.open(str(dest), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(amplify_pcm(b"".join(pcm_chunks)))
    info.update(
        {
            "ok": True,
            "error": None,
            "seconds": round(frames / SAMPLE_RATE, 1),
            "rssi_dbm": round(last_rssi, 1) if last_rssi is not None else None,
        }
    )
    log.info("WAV %s (%.1f s, RSSI %s, %s SND)", dest.name, info["seconds"], info["rssi_dbm"], snd_n)
    return info
