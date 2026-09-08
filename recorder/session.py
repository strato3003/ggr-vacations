"""Orchestration d'une vacation : flotte → KiwiSDR → audio + screencast."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from recorder.config import data_dir, load_config, version
from recorder.fleet import fetch_fleet
from recorder.geo import fmt_latlon
from recorder.kiwi_audio import record_kiwi_wav
from recorder.kiwi_list import fetch_ranked_kiwis
from recorder.postprocess import mux_screencast, thumbnail
from recorder.screencast import record_screencast

log = logging.getLogger(__name__)
LOCK_NAME = ".recording.lock"


def vacation_id(when: datetime | None = None) -> str:
    when = when or datetime.now(timezone.utc)
    return when.strftime("%Y-%m-%dT%H%MZ")


def _channels(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    radio = cfg.get("radio") or {}
    tx = radio.get("tx") or {}
    rows = [
        {
            "id": "tx",
            "kind": "tx",
            "freq_khz": float(tx["freq_khz"]),
            "label": tx.get("label") or "Bulletin F6KUF",
            "zoom": int(tx.get("zoom") or 10),
            "screencast": bool((cfg.get("sdr") or {}).get("screencast_tx", True)),
        }
    ]
    for idx, ack in enumerate(radio.get("ack") or []):
        rows.append(
            {
                "id": f"ack{idx + 1}",
                "kind": "ack",
                "freq_khz": float(ack["freq_khz"]),
                "label": ack.get("label") or f"Accusé {ack['freq_khz']} kHz",
                "zoom": int(ack.get("zoom") or 10),
                "screencast": bool((cfg.get("sdr") or {}).get("screencast_ack", False)),
            }
        )
    return rows


def _pick_kiwis(ranked: list[dict[str, Any]], channels: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Répartit les canaux : TX sur le meilleur, ACK sur le suivant si possible."""
    assignment: dict[str, dict[str, Any]] = {}
    if not ranked:
        return assignment
    assignment["tx"] = ranked[0]
    alt = ranked[1] if len(ranked) > 1 else ranked[0]
    for ch in channels:
        if ch["id"] == "tx":
            continue
        assignment[ch["id"]] = alt
    return assignment


def next_vacation_utc(cfg: dict[str, Any], now: datetime | None = None) -> datetime:
    sched = cfg.get("schedule") or {}
    hh, mm = (sched.get("time_utc") or "18:00").split(":")
    lead = int(sched.get("lead_minutes") or 10)
    now = now or datetime.now(timezone.utc)
    start = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0) - timedelta(minutes=lead)
    if start <= now:
        start = start + timedelta(days=1)
    return start


async def run_vacation(cfg: dict[str, Any] | None = None, *, reason: str = "schedule") -> dict[str, Any]:
    cfg = cfg or load_config()
    root = data_dir(cfg)
    lock = root / LOCK_NAME
    if lock.exists():
        raise RuntimeError("Un enregistrement est déjà en cours")
    lock.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
    started = datetime.now(timezone.utc)
    vid = vacation_id(started)
    session_dir = root / "vacations" / vid
    session_dir.mkdir(parents=True, exist_ok=True)
    meta: dict[str, Any] = {
        "id": vid,
        "status": "running",
        "reason": reason,
        "version": version(cfg),
        "club": cfg.get("club"),
        "started_at": started.isoformat(),
        "schedule": cfg.get("schedule"),
        "radio": cfg.get("radio"),
        "channels": [],
    }
    _write_meta(session_dir, meta)

    try:
        fleet = await fetch_fleet(cfg)
        meta["fleet"] = fleet
        ranked = await fetch_ranked_kiwis(cfg, fleet["lat"], fleet["lon"])
        meta["kiwis_ranked"] = ranked[:8]
        channels = _channels(cfg)
        assignment = _pick_kiwis(ranked, channels)
        if not assignment:
            raise RuntimeError("Aucun KiwiSDR disponible pour la position de la flotte")

        duration = int((cfg.get("schedule") or {}).get("duration_minutes") or 45) * 60
        radio = cfg.get("radio") or {}
        filt = radio.get("usb_filter") or {}
        ident = (cfg.get("sdr") or {}).get("ident_user") or "ggr-vacations"
        viewport = (cfg.get("sdr") or {}).get("viewport") or {"width": 1280, "height": 800}
        when_label = started.strftime("%Y-%m-%d %H:%M")

        jobs = []
        for ch in channels:
            kiwi = assignment.get(ch["id"]) or assignment["tx"]
            ch_out = {
                **ch,
                "kiwi": {
                    "name": kiwi.get("name"),
                    "url": kiwi.get("url"),
                    "distance_km": kiwi.get("distance_km"),
                    "fmt": kiwi.get("fmt"),
                    "snr_hf": kiwi.get("snr_hf"),
                },
            }
            wav = session_dir / f"audio-{ch['id']}.wav"
            jobs.append(
                record_kiwi_wav(
                    kiwi,
                    ch["freq_khz"],
                    wav,
                    duration,
                    mode=str(radio.get("mode") or "usb"),
                    low_hz=int(filt.get("low_hz") or 300),
                    high_hz=int(filt.get("high_hz") or 2700),
                    ident=ident,
                )
            )
            ch_out["audio"] = str(wav.name)
            if ch.get("screencast"):
                webm = session_dir / f"screencast-{ch['id']}.webm"
                overlay = {
                    "when": when_label,
                    "channel": ch["label"],
                    "freq": f"{ch['freq_khz']:.4f} kHz USB",
                    "kiwi": kiwi.get("name"),
                }
                jobs.append(
                    record_screencast(
                        kiwi,
                        ch["freq_khz"],
                        webm,
                        duration,
                        mode=str(radio.get("mode") or "usb"),
                        zoom=int(ch.get("zoom") or 10),
                        viewport=viewport,
                        overlay=overlay,
                    )
                )
                ch_out["screencast_raw"] = str(webm.name)
            meta["channels"].append(ch_out)

        results = await asyncio.gather(*jobs, return_exceptions=True)
        meta["raw_results"] = [
            (repr(r) if isinstance(r, Exception) else r) for r in results
        ]

        for ch in meta["channels"]:
            wav = session_dir / ch["audio"]
            raw = session_dir / ch.get("screencast_raw", "")
            if ch.get("screencast_raw") and raw.exists():
                mp4 = session_dir / f"screencast-{ch['id']}.mp4"
                if mux_screencast(raw, wav, mp4):
                    ch["video"] = mp4.name
                    thumb = session_dir / f"thumb-{ch['id']}.jpg"
                    if thumbnail(mp4, thumb):
                        ch["thumb"] = thumb.name
                    try:
                        raw.unlink()
                    except OSError:
                        pass

        meta["status"] = "complete"
        meta["ended_at"] = datetime.now(timezone.utc).isoformat()
        meta["fleet_fmt"] = fleet.get("fmt") or fmt_latlon(fleet["lat"], fleet["lon"])
        log.info("Vacation %s terminée", vid)
        return meta
    except Exception as exc:
        meta["status"] = "error"
        meta["error"] = str(exc)
        meta["ended_at"] = datetime.now(timezone.utc).isoformat()
        log.exception("Vacation %s en échec", vid)
        return meta
    finally:
        _write_meta(session_dir, meta)
        try:
            lock.unlink()
        except OSError:
            pass


# Segment téléphonie 20 m (USB, IARU R1 ~ 14,150–14,350 kHz) — scan pour capter un QSO.
SCAN_20M_KHZ = (14190.0, 14200.0, 14210.0, 14220.0, 14230.0, 14245.0, 14260.0, 14275.0)
SCAN_20M_DWELL_S = 20.0


async def run_test_20m(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Scan USB 20 m + screencast, archivé comme une vacation visible dans l’UI."""
    cfg = cfg or load_config()
    root = data_dir(cfg)
    lock = root / LOCK_NAME
    if lock.exists():
        raise RuntimeError("Un enregistrement est déjà en cours")
    lock.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
    started = datetime.now(timezone.utc)
    vid = started.strftime("%Y-%m-%dT%H%MZ") + "-20m"
    session_dir = root / "vacations" / vid
    session_dir.mkdir(parents=True, exist_ok=True)
    plan = [(f, SCAN_20M_DWELL_S) for f in SCAN_20M_KHZ]
    duration = sum(d for _, d in plan)
    radio = cfg.get("radio") or {}
    filt = radio.get("usb_filter") or {}
    ident = (cfg.get("sdr") or {}).get("ident_user") or "ggr-vacations"
    viewport = (cfg.get("sdr") or {}).get("viewport") or {"width": 1280, "height": 800}
    meta: dict[str, Any] = {
        "id": vid,
        "status": "running",
        "reason": "test-20m",
        "title": "Test scan 20 m USB",
        "version": version(cfg),
        "club": cfg.get("club"),
        "started_at": started.isoformat(),
        "scan_khz": list(SCAN_20M_KHZ),
        "channels": [],
    }
    _write_meta(session_dir, meta)
    try:
        fleet = await fetch_fleet(cfg)
        meta["fleet"] = fleet
        ranked = await fetch_ranked_kiwis(cfg, fleet["lat"], fleet["lon"])
        if not ranked:
            raise RuntimeError("Aucun KiwiSDR disponible pour le scan 20 m")
        kiwi = ranked[0]
        meta["kiwis_ranked"] = ranked[:8]
        ch_out = {
            "id": "tx",
            "kind": "scan",
            "freq_khz": SCAN_20M_KHZ[0],
            "label": "Scan 20 m USB (14,19–14,275 MHz)",
            "zoom": 8,
            "screencast": True,
            "audio": "audio-tx.wav",
            "screencast_raw": "screencast-tx.webm",
            "kiwi": {
                "name": kiwi.get("name"),
                "url": kiwi.get("url"),
                "distance_km": kiwi.get("distance_km"),
                "fmt": kiwi.get("fmt"),
                "snr_hf": kiwi.get("snr_hf"),
            },
        }
        meta["channels"].append(ch_out)
        overlay = {
            "when": started.strftime("%Y-%m-%d %H:%M"),
            "channel": "Test scan 20 m",
            "freq": f"{SCAN_20M_KHZ[0]:.2f} kHz USB",
            "kiwi": kiwi.get("name"),
        }
        wav = session_dir / "audio-tx.wav"
        webm = session_dir / "screencast-tx.webm"
        results = await asyncio.gather(
            record_kiwi_wav(
                kiwi,
                SCAN_20M_KHZ[0],
                wav,
                duration,
                mode="usb",
                low_hz=int(filt.get("low_hz") or 300),
                high_hz=int(filt.get("high_hz") or 2700),
                ident=ident,
                freq_plan=plan,
            ),
            record_screencast(
                kiwi,
                SCAN_20M_KHZ[0],
                webm,
                duration,
                mode="usb",
                zoom=8,
                viewport=viewport,
                overlay=overlay,
                freq_plan=plan,
            ),
            return_exceptions=True,
        )
        meta["raw_results"] = [
            (repr(r) if isinstance(r, Exception) else r) for r in results
        ]
        if webm.exists():
            mp4 = session_dir / "screencast-tx.mp4"
            if mux_screencast(webm, wav, mp4):
                ch_out["video"] = mp4.name
                thumb = session_dir / "thumb-tx.jpg"
                if thumbnail(mp4, thumb, at_s=30):
                    ch_out["thumb"] = thumb.name
                webm.unlink(missing_ok=True)
        meta["status"] = "complete"
        meta["ended_at"] = datetime.now(timezone.utc).isoformat()
        meta["fleet_fmt"] = fleet.get("fmt") or fmt_latlon(fleet["lat"], fleet["lon"])
        log.info("Test 20 m %s terminé", vid)
        return meta
    except Exception as exc:
        meta["status"] = "error"
        meta["error"] = str(exc)
        meta["ended_at"] = datetime.now(timezone.utc).isoformat()
        log.exception("Test 20 m %s en échec", vid)
        return meta
    finally:
        _write_meta(session_dir, meta)
        try:
            lock.unlink()
        except OSError:
            pass


def _write_meta(session_dir: Path, meta: dict[str, Any]) -> None:
    path = session_dir / "metadata.json"
    tmp = session_dir / "metadata.json.tmp"
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def purge_old(cfg: dict[str, Any] | None = None) -> int:
    cfg = cfg or load_config()
    days = int((cfg.get("storage") or {}).get("retention_days") or 180)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    root = data_dir(cfg) / "vacations"
    if not root.exists():
        return 0
    removed = 0
    for folder in root.iterdir():
        if not folder.is_dir():
            continue
        meta_path = folder / "metadata.json"
        stamp = datetime.fromtimestamp(folder.stat().st_mtime, tz=timezone.utc)
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                stamp = datetime.fromisoformat(meta.get("started_at", "").replace("Z", "+00:00"))
            except (OSError, ValueError, json.JSONDecodeError):
                pass
        if stamp < cutoff:
            for child in folder.glob("*"):
                child.unlink(missing_ok=True)
            folder.rmdir()
            removed += 1
    return removed


def main() -> None:
    parser = argparse.ArgumentParser(description="Enregistrer une vacation HF GGR / F6KUF")
    parser.add_argument("--once", action="store_true", help="Lancer un enregistrement immédiat (vacation F6KUF)")
    parser.add_argument("--test-20m", action="store_true", help="Scan USB 20 m (test), puis archive dans l’UI")
    parser.add_argument("--config", default=os.environ.get("GGR_CONFIG"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config(args.config)
    if args.test_20m:
        meta = asyncio.run(run_test_20m(cfg))
        print(json.dumps({"id": meta.get("id"), "status": meta.get("status"), "error": meta.get("error")}, ensure_ascii=False))
        if meta.get("status") != "complete":
            raise SystemExit(1)
        return
    if args.once:
        meta = asyncio.run(run_vacation(cfg, reason="manual"))
        print(json.dumps({"id": meta.get("id"), "status": meta.get("status"), "error": meta.get("error")}, ensure_ascii=False))
        if meta.get("status") != "complete":
            raise SystemExit(1)
        return
    parser.print_help()


if __name__ == "__main__":
    main()
