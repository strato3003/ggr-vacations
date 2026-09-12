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

from recorder.config import data_dir, fmt_khz, fmt_mhz, load_config, version
from recorder.fleet import buddy_aim, fetch_fleet
from recorder.geo import fmt_latlon
from recorder.kiwi_audio import record_kiwi_wav
from recorder.kiwi_list import assign_buddy_kiwis, assign_vacation_kiwis, fetch_ranked_kiwis, pick_nearest
from recorder.postprocess import mux_screencast, thumbnail
from recorder.screencast import record_screencast
from recorder.kiwi_wf import HUNT_CF_KHZ, HUNT_HI_KHZ, HUNT_LO_KHZ, HUNT_ZOOM, hunt_usb_signal

log = logging.getLogger(__name__)
LOCK_NAME = ".recording.lock"
RECORDING_GRACE_S = 180
ORPHAN_ERROR = "Enregistrement interrompu (processus arrêté avant la fin)"


def vacation_id(when: datetime | None = None) -> str:
    when = when or datetime.now(timezone.utc)
    return when.strftime("%Y-%m-%dT%H%MZ")


def _channels(cfg: dict[str, Any], ack_sites: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    radio = cfg.get("radio") or {}
    tx = radio.get("tx") or {}
    rows = [
        {
            "id": "tx",
            "kind": "tx",
            "site": "tx",
            "site_label": "flotte (bulletin)",
            "freq_khz": float(tx["freq_khz"]),
            "label": tx.get("label") or "Bulletin F6KUF",
            "zoom": int(tx.get("zoom") or 10),
            "screencast": bool((cfg.get("sdr") or {}).get("screencast_tx", True)),
        }
    ]
    sites = ack_sites or []
    for idx, ack in enumerate(radio.get("ack") or []):
        base_label = ack.get("label") or f"Accusé {ack['freq_khz']} kHz"
        if not sites:
            rows.append(
                {
                    "id": f"ack{idx + 1}",
                    "kind": "ack",
                    "site": "fleet",
                    "site_label": "flotte",
                    "freq_khz": float(ack["freq_khz"]),
                    "label": base_label,
                    "zoom": int(ack.get("zoom") or 10),
                    "screencast": bool((cfg.get("sdr") or {}).get("screencast_ack", False)),
                }
            )
            continue
        for site in sites:
            sid = str(site["id"])
            slabel = site.get("label") or sid
            rows.append(
                {
                    "id": f"ack{idx + 1}-{sid}",
                    "kind": "ack",
                    "site": sid,
                    "site_label": slabel,
                    "freq_khz": float(ack["freq_khz"]),
                    "label": f"{base_label} · {slabel}",
                    "zoom": int(ack.get("zoom") or 10),
                    "screencast": bool((cfg.get("sdr") or {}).get("screencast_ack", False)),
                }
            )
    return rows


def _pick_kiwis(
    roles: dict[str, dict[str, Any]],
    channels: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Associe chaque canal au Kiwi du rôle (tx / fleet / france / tahiti)."""
    assignment: dict[str, dict[str, Any]] = {}
    tx = roles.get("tx")
    for ch in channels:
        if ch["id"] == "tx":
            if tx:
                assignment["tx"] = tx
            continue
        kiwi = roles.get(str(ch.get("site") or ""))
        if kiwi:
            assignment[ch["id"]] = kiwi
    return assignment


def next_vacation_utc(cfg: dict[str, Any], now: datetime | None = None) -> datetime:
    sched = cfg.get("schedule") or {}
    hh, mm = (sched.get("time_utc") or "18:00").split(":")
    lead = int(sched.get("lead_minutes") or 1)
    now = now or datetime.now(timezone.utc)
    start = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0) - timedelta(minutes=lead)
    if start <= now:
        start = start + timedelta(days=1)
    return start


def next_buddy_utc(cfg: dict[str, Any], now: datetime | None = None) -> datetime:
    buddy = cfg.get("buddy") or {}
    hh, mm = str(buddy.get("time_utc") or "12:00").split(":")
    lead = int(buddy.get("lead_minutes") or 1)
    now = now or datetime.now(timezone.utc)
    start = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0) - timedelta(minutes=lead)
    if start <= now:
        start = start + timedelta(days=1)
    return start


async def _hunt_usb_around(
    cfg: dict[str, Any],
    kiwi: dict[str, Any],
    nominal_khz: float,
    tol_khz: float,
) -> dict[str, Any] | None:
    """Blob USB dans ±tolérance autour de nominal (filtre USB au-dessus du VFO)."""
    radio = cfg.get("radio") or {}
    filt = radio.get("usb_filter") or {}
    ident = (cfg.get("sdr") or {}).get("ident_user") or "ggr-vacations"
    low_hz = int(filt.get("low_hz") or 300)
    high_hz = int(filt.get("high_hz") or 2700)
    return await hunt_usb_signal(
        kiwi,
        lo_khz=nominal_khz - tol_khz,
        hi_khz=nominal_khz + tol_khz + (high_hz / 1000.0),
        ident=ident,
        low_hz=low_hz,
        high_hz=high_hz,
        cf_khz=nominal_khz,
    )


async def _follow_tx_qrg(
    cfg: dict[str, Any],
    kiwi: dict[str, Any],
    channels: list[dict[str, Any]],
) -> None:
    """Si un blob USB est dans ±tolérance, recale le VFO TX (sinon QRG nominale)."""
    radio = cfg.get("radio") or {}
    tx = radio.get("tx") or {}
    nominal = float(tx.get("freq_khz") or 14135.0)
    tol = float(tx.get("qrg_tolerance_khz") or 5.0)
    hit = await _hunt_usb_around(cfg, kiwi, nominal, tol)
    for ch in channels:
        if ch["id"] != "tx":
            continue
        ch["freq_nominal_khz"] = nominal
        ch["qrg_tolerance_khz"] = tol
        if not hit:
            log.info(
                "Pas de blob USB autour de %.4f kHz (±%s kHz) — accord nominal",
                nominal,
                tol,
            )
            return
        found = float(hit["freq_khz"])
        ch["freq_khz"] = found
        ch["hunt"] = {k: hit[k] for k in hit if k != "kiwi"}
        log.info("Suivi QRG TX : %.4f → %.4f kHz", nominal, found)
        return


async def run_manual_qrg(
    cfg: dict[str, Any] | None = None,
    *,
    freq_khz: float,
    duration_minutes: int = 2,
    hunt: bool = True,
    qrg_tolerance_khz: float | None = None,
) -> dict[str, Any]:
    """Screencast + audio USB sur une QRG libre (test immédiat, Kiwi le plus proche de la flotte)."""
    cfg = cfg or load_config()
    root = data_dir(cfg)
    lock = root / LOCK_NAME
    if lock.exists():
        raise RuntimeError("Un enregistrement est déjà en cours")
    lock.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
    started = datetime.now(timezone.utc)
    vid = started.strftime("%Y-%m-%dT%H%MZ") + "-qrg"
    session_dir = root / "vacations" / vid
    session_dir.mkdir(parents=True, exist_ok=True)
    radio = cfg.get("radio") or {}
    tx = radio.get("tx") or {}
    tol = float(qrg_tolerance_khz if qrg_tolerance_khz is not None else tx.get("qrg_tolerance_khz") or 5.0)
    minutes = max(1, int(duration_minutes))
    duration = minutes * 60
    mhz = fmt_mhz(freq_khz)
    viewport = (cfg.get("sdr") or {}).get("viewport") or {"width": 1280, "height": 800}
    meta: dict[str, Any] = {
        "id": vid,
        "status": "running",
        "reason": "manual-qrg",
        "title": f"Record {mhz} MHz USB",
        "version": version(cfg),
        "club": cfg.get("club"),
        "started_at": started.isoformat(),
        "duration_minutes": minutes,
        "freq_nominal_khz": freq_khz,
        "qrg_tolerance_khz": tol,
        "hunt": hunt,
        "channels": [],
    }
    _write_meta(session_dir, meta)
    try:
        fleet = await fetch_fleet(cfg)
        meta["fleet"] = fleet
        ranked = await fetch_ranked_kiwis(cfg, fleet["lat"], fleet["lon"], limit=0, min_free=1)
        meta["kiwis_ranked"] = ranked[:8]
        tx_slots = int((cfg.get("sdr") or {}).get("min_free_slots") or 2)
        kiwi = pick_nearest(
            ranked,
            float(fleet["lat"]),
            float(fleet["lon"]),
            min_free=tx_slots,
            min_snr=5.0,
        )
        if kiwi is None:
            kiwi = pick_nearest(ranked, float(fleet["lat"]), float(fleet["lon"]), min_free=1, min_snr=0.0)
        if kiwi is None:
            raise RuntimeError("Aucun KiwiSDR disponible pour ce record")
        tuned = float(freq_khz)
        hunt_info = None
        if hunt:
            try:
                hit = await _hunt_usb_around(cfg, kiwi, tuned, tol)
            except Exception:
                log.exception("Chasse QRG %.4f kHz impossible", tuned)
                hit = None
            if hit:
                tuned = float(hit["freq_khz"])
                hunt_info = {k: hit[k] for k in hit if k != "kiwi"}
                log.info("Record manuel : %.4f → %.4f kHz", freq_khz, tuned)
            else:
                log.info("Record manuel : pas de blob USB autour de %.4f kHz (±%s) — QRG demandée", freq_khz, tol)
        wav = session_dir / "audio-tx.wav"
        webm = session_dir / "screencast-tx.webm"
        overlay = {
            "when": started.strftime("%Y-%m-%d %H:%M"),
            "channel": f"Record {mhz} MHz",
            "freq": f"{tuned:.4f} kHz USB",
            "kiwi": kiwi.get("name"),
        }
        raw = await record_screencast(
            kiwi,
            tuned,
            webm,
            duration,
            mode=str(radio.get("mode") or "usb"),
            zoom=int(tx.get("zoom") or 10),
            viewport=viewport,
            overlay=overlay,
            snd_wav=wav,
        )
        ch_out = {
            "id": "tx",
            "kind": "manual",
            "freq_khz": tuned,
            "freq_nominal_khz": freq_khz,
            "qrg_tolerance_khz": tol,
            "label": f"Record {fmt_mhz(tuned)} MHz USB",
            "zoom": int(tx.get("zoom") or 10),
            "screencast": True,
            "screencast_raw": "screencast-tx.webm",
            "audio_file": "audio-tx.wav",
            "hunt": hunt_info,
            "kiwi": {
                "name": kiwi.get("name"),
                "url": kiwi.get("url"),
                "distance_km": kiwi.get("distance_km"),
                "fmt": kiwi.get("fmt"),
                "snr_hf": kiwi.get("snr_hf"),
                "site_km": kiwi.get("distance_km"),
            },
        }
        if isinstance(raw, dict) and isinstance(raw.get("audio_delay_s"), (int, float)):
            ch_out["audio_delay_s"] = round(float(raw["audio_delay_s"]), 3)
        meta["channels"].append(ch_out)
        meta["raw_results"] = [raw if not isinstance(raw, Exception) else repr(raw)]
        if isinstance(raw, Exception):
            raise raw
        meta["status"] = "complete"
        meta["ended_at"] = datetime.now(timezone.utc).isoformat()
        meta["fleet_fmt"] = fleet.get("fmt") or fmt_latlon(fleet["lat"], fleet["lon"])
        log.info("Record QRG %s terminé", vid)
        return meta
    except Exception as exc:
        meta["status"] = "error"
        meta["error"] = str(exc)
        meta["ended_at"] = datetime.now(timezone.utc).isoformat()
        log.exception("Record QRG %s en échec", vid)
        return meta
    finally:
        _finalize_media(session_dir, meta)
        _write_meta(session_dir, meta)
        try:
            lock.unlink()
        except OSError:
            pass


async def run_vacation(
    cfg: dict[str, Any] | None = None,
    *,
    reason: str = "schedule",
    duration_minutes: int | None = None,
) -> dict[str, Any]:
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
        ranked = await fetch_ranked_kiwis(cfg, fleet["lat"], fleet["lon"], limit=0, min_free=1)
        meta["kiwis_ranked"] = ranked[:8]
        roles = assign_vacation_kiwis(
            ranked,
            fleet_lat=float(fleet["lat"]),
            fleet_lon=float(fleet["lon"]),
            cfg=cfg,
        )
        meta["kiwi_roles"] = {
            role: {
                "name": kiwi.get("name"),
                "url": kiwi.get("url"),
                "fmt": kiwi.get("fmt"),
                "site_km": kiwi.get("site_km"),
                "snr_hf": kiwi.get("snr_hf"),
            }
            for role, kiwi in roles.items()
        }
        ack_sites = [
            {"id": sid, "label": roles[sid].get("site_label") or sid}
            for sid in ("fleet", "france", "tahiti")
            if sid in roles
        ]
        channels = _channels(cfg, ack_sites)
        assignment = _pick_kiwis(roles, channels)
        if "tx" not in assignment:
            raise RuntimeError("Aucun KiwiSDR disponible pour la position de la flotte")

        tx_kiwi = assignment.get("tx") or ranked[0]
        try:
            await _follow_tx_qrg(cfg, tx_kiwi, channels)
        except Exception:
            log.exception("Suivi QRG TX impossible — accord nominal")

        minutes = (
            duration_minutes
            if duration_minutes is not None
            else int((cfg.get("schedule") or {}).get("duration_minutes") or 10)
        )
        duration = int(minutes) * 60
        meta["duration_minutes"] = int(minutes)
        radio = cfg.get("radio") or {}
        filt = radio.get("usb_filter") or {}
        ident = (cfg.get("sdr") or {}).get("ident_user") or "ggr-vacations"
        viewport = (cfg.get("sdr") or {}).get("viewport") or {"width": 1280, "height": 800}
        when_label = started.strftime("%Y-%m-%d %H:%M")

        jobs = []
        for ch in channels:
            kiwi = assignment.get(ch["id"])
            if not kiwi:
                log.warning("Canal %s sans Kiwi — ignoré", ch["id"])
                continue
            ch_out = {
                **ch,
                "kiwi": {
                    "name": kiwi.get("name"),
                    "url": kiwi.get("url"),
                    "distance_km": kiwi.get("distance_km"),
                    "fmt": kiwi.get("fmt"),
                    "snr_hf": kiwi.get("snr_hf"),
                    "site_km": kiwi.get("site_km"),
                },
            }
            wav = session_dir / f"audio-{ch['id']}.wav"
            ch_out["audio_file"] = str(wav.name)
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
                        snd_wav=wav,
                    )
                )
                ch_out["screencast_raw"] = str(webm.name)
            else:
                jobs.append(
                    record_kiwi_wav(
                        kiwi,
                        ch["freq_khz"],
                        wav,
                        duration,
                        mode=str(radio.get("mode") or "usb"),
                        low_hz=int(filt.get("low_hz") or 300),
                        high_hz=int(filt.get("high_hz") or 2700),
                        ident=f"{ident}-{ch.get('site') or ch['id']}" if ch.get("kind") == "ack" else ident,
                    )
                )
            meta["channels"].append(ch_out)

        _write_meta(session_dir, meta)
        try:
            results = await asyncio.wait_for(
                asyncio.gather(*jobs, return_exceptions=True),
                timeout=duration + RECORDING_GRACE_S,
            )
        except TimeoutError:
            log.error(
                "Vacation %s : timeout après %ss",
                vid,
                duration + RECORDING_GRACE_S,
            )
            raise RuntimeError(
                f"Timeout après {int((duration + RECORDING_GRACE_S) / 60)} min "
                "(screencast ou audio bloqué)"
            ) from None
        meta["raw_results"] = [
            (repr(r) if isinstance(r, Exception) else r) for r in results
        ]
        for ch, raw in zip(meta["channels"], results):
            if isinstance(raw, dict) and isinstance(raw.get("audio_delay_s"), (int, float)):
                ch["audio_delay_s"] = round(float(raw["audio_delay_s"]), 3)

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
        _finalize_media(session_dir, meta)
        _write_meta(session_dir, meta)
        try:
            lock.unlink()
        except OSError:
            pass


def _buddy_channels(cfg: dict[str, Any], roles: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    buddy = cfg.get("buddy") or {}
    main = buddy.get("main") or {}
    alt = buddy.get("alternate") or {}
    main_khz = float(main.get("freq_khz") or 4483.0)
    alt_khz = float(alt.get("freq_khz") or 6516.0)
    main_label = main.get("label") or f"Buddy call {fmt_khz(main_khz)} kHz"
    alt_label = alt.get("label") or f"Buddy call {fmt_khz(alt_khz)} kHz (secours)"
    main_zoom = int(main.get("zoom") or 10)
    alt_zoom = int(alt.get("zoom") or 10)
    rows: list[dict[str, Any]] = []
    first = True
    for site, kiwi in roles.items():
        slabel = kiwi.get("site_label") or site
        need_two = int(kiwi.get("free_slots") or 0) >= 2
        rows.append(
            {
                "id": f"{site}-main",
                "kind": "buddy-main",
                "site": site,
                "site_label": slabel,
                "freq_khz": main_khz,
                "label": f"{main_label} · {slabel}",
                "zoom": main_zoom,
                "screencast": first,
            }
        )
        first = False
        if need_two:
            rows.append(
                {
                    "id": f"{site}-alt",
                    "kind": "buddy-alt",
                    "site": site,
                    "site_label": slabel,
                    "freq_khz": alt_khz,
                    "label": f"{alt_label} · {slabel}",
                    "zoom": alt_zoom,
                    "screencast": False,
                }
            )
        else:
            log.info("Kiwi %s : une seule place — 4483 kHz seulement (pas 6516)", kiwi.get("name"))
    return rows


async def run_buddy_call(
    cfg: dict[str, Any] | None = None,
    *,
    reason: str = "buddy",
    duration_minutes: int | None = None,
) -> dict[str, Any]:
    """Écoute quotidienne 12:00 TU : 4483 kHz + 6516 kHz sur plusieurs Kiwi."""
    cfg = cfg or load_config()
    buddy = cfg.get("buddy") or {}
    root = data_dir(cfg)
    lock = root / LOCK_NAME
    if lock.exists():
        raise RuntimeError("Un enregistrement est déjà en cours")
    lock.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
    started = datetime.now(timezone.utc)
    vid = vacation_id(started) + "-buddy"
    session_dir = root / "vacations" / vid
    session_dir.mkdir(parents=True, exist_ok=True)
    main_khz = float((buddy.get("main") or {}).get("freq_khz") or 4483.0)
    alt_khz = float((buddy.get("alternate") or {}).get("freq_khz") or 6516.0)
    cover_hz = [int(round(main_khz * 1000.0)), int(round(alt_khz * 1000.0))]
    min_free = int(((buddy.get("kiwi") or {}).get("min_free_slots") or 2))
    meta: dict[str, Any] = {
        "id": vid,
        "status": "running",
        "reason": reason,
        "title": f"Buddy call {fmt_khz(main_khz)} / {fmt_khz(alt_khz)} kHz",
        "version": version(cfg),
        "club": cfg.get("club"),
        "started_at": started.isoformat(),
        "buddy": buddy,
        "channels": [],
    }
    _write_meta(session_dir, meta)
    try:
        fleet = await fetch_fleet(cfg)
        aim = buddy_aim(fleet, cfg)
        meta["fleet"] = fleet
        meta["buddy_aim"] = aim
        ranked = await fetch_ranked_kiwis(
            cfg,
            float(aim["lat"]),
            float(aim["lon"]),
            limit=0,
            min_free=min_free,
            cover_hz=cover_hz,
            score_mode="buddy",
        )
        if len(ranked) < 2:
            ranked = await fetch_ranked_kiwis(
                cfg,
                float(aim["lat"]),
                float(aim["lon"]),
                limit=0,
                min_free=1,
                cover_hz=cover_hz,
                score_mode="buddy",
            )
        meta["kiwis_ranked"] = ranked[:10]
        roles = assign_buddy_kiwis(ranked, lat=float(aim["lat"]), lon=float(aim["lon"]), cfg=cfg)
        if not roles:
            raise RuntimeError("Aucun KiwiSDR couvrant 4483 et 6516 kHz vers le centroïde buddy")
        meta["kiwi_roles"] = {
            role: {
                "name": kiwi.get("name"),
                "url": kiwi.get("url"),
                "fmt": kiwi.get("fmt"),
                "site_km": kiwi.get("site_km"),
                "snr_hf": kiwi.get("snr_hf"),
                "prop_zone": kiwi.get("prop_zone"),
                "site_label": kiwi.get("site_label"),
            }
            for role, kiwi in roles.items()
        }
        channels = _buddy_channels(cfg, roles)
        assignment = {ch["id"]: roles[str(ch["site"])] for ch in channels if str(ch.get("site")) in roles}
        minutes = (
            duration_minutes
            if duration_minutes is not None
            else int(buddy.get("duration_minutes") or 15)
        )
        duration = int(minutes) * 60
        meta["duration_minutes"] = int(minutes)
        radio = cfg.get("radio") or {}
        filt = radio.get("usb_filter") or {}
        ident = (cfg.get("sdr") or {}).get("ident_user") or "ggr-vacations"
        viewport = (cfg.get("sdr") or {}).get("viewport") or {"width": 1280, "height": 800}
        when_label = started.strftime("%Y-%m-%d %H:%M")
        mode = str(buddy.get("mode") or radio.get("mode") or "usb")
        jobs = []
        for ch in channels:
            kiwi = assignment.get(ch["id"])
            if not kiwi:
                continue
            ch_out = {
                **ch,
                "kiwi": {
                    "name": kiwi.get("name"),
                    "url": kiwi.get("url"),
                    "distance_km": kiwi.get("distance_km"),
                    "fmt": kiwi.get("fmt"),
                    "snr_hf": kiwi.get("snr_hf"),
                    "site_km": kiwi.get("site_km"),
                    "prop_zone": kiwi.get("prop_zone"),
                },
            }
            wav = session_dir / f"audio-{ch['id']}.wav"
            ch_out["audio_file"] = str(wav.name)
            if ch.get("screencast"):
                webm = session_dir / f"screencast-{ch['id']}.webm"
                overlay = {
                    "when": when_label,
                    "channel": ch["label"],
                    "freq": f"{fmt_khz(ch['freq_khz'])} kHz USB",
                    "kiwi": kiwi.get("name"),
                }
                jobs.append(
                    record_screencast(
                        kiwi,
                        ch["freq_khz"],
                        webm,
                        duration,
                        mode=mode,
                        zoom=int(ch.get("zoom") or 10),
                        viewport=viewport,
                        overlay=overlay,
                        snd_wav=wav,
                    )
                )
                ch_out["screencast_raw"] = str(webm.name)
            else:
                jobs.append(
                    record_kiwi_wav(
                        kiwi,
                        ch["freq_khz"],
                        wav,
                        duration,
                        mode=mode,
                        low_hz=int(filt.get("low_hz") or 300),
                        high_hz=int(filt.get("high_hz") or 2700),
                        ident=f"{ident}-buddy-{ch.get('site') or ch['id']}",
                    )
                )
            meta["channels"].append(ch_out)
        if not jobs:
            raise RuntimeError("Aucun canal buddy à enregistrer")
        _write_meta(session_dir, meta)
        try:
            results = await asyncio.wait_for(
                asyncio.gather(*jobs, return_exceptions=True),
                timeout=duration + RECORDING_GRACE_S,
            )
        except TimeoutError:
            raise RuntimeError(
                f"Timeout buddy après {int((duration + RECORDING_GRACE_S) / 60)} min"
            ) from None
        meta["raw_results"] = [(repr(r) if isinstance(r, Exception) else r) for r in results]
        for ch, raw in zip(meta["channels"], results):
            if isinstance(raw, dict) and isinstance(raw.get("audio_delay_s"), (int, float)):
                ch["audio_delay_s"] = round(float(raw["audio_delay_s"]), 3)
        meta["status"] = "complete"
        meta["ended_at"] = datetime.now(timezone.utc).isoformat()
        meta["fleet_fmt"] = aim.get("fmt") or fmt_latlon(aim["lat"], aim["lon"])
        log.info("Buddy call %s terminé (%s Kiwi)", vid, len(roles))
        return meta
    except Exception as exc:
        meta["status"] = "error"
        meta["error"] = str(exc)
        meta["ended_at"] = datetime.now(timezone.utc).isoformat()
        log.exception("Buddy call %s en échec", vid)
        return meta
    finally:
        _finalize_media(session_dir, meta)
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
        last_err = None
        wav = session_dir / "audio-tx.wav"
        webm = session_dir / "screencast-tx.webm"
        results = []
        used = None
        for kiwi in ranked[:4]:
            overlay = {
                "when": started.strftime("%Y-%m-%d %H:%M"),
                "channel": "Test scan 20 m",
                "freq": f"{SCAN_20M_KHZ[0]:.2f} kHz USB",
                "kiwi": kiwi.get("name"),
            }
            wav.unlink(missing_ok=True)
            webm.unlink(missing_ok=True)
            try:
                results = [
                    await record_screencast(
                        kiwi,
                        SCAN_20M_KHZ[0],
                        webm,
                        duration,
                        mode="usb",
                        zoom=8,
                        viewport=viewport,
                        overlay=overlay,
                        freq_plan=plan,
                        snd_wav=wav,
                    )
                ]
            except Exception as exc:
                last_err = str(exc)
                log.warning("Kiwi %s : %s", kiwi.get("name"), exc)
                continue
            raw = results[0]
            if isinstance(raw, dict) and raw.get("snd_packets"):
                used = kiwi
                break
            last_err = raw.get("error") if isinstance(raw, dict) else str(raw)
            log.warning("Scan 20 m sans audio sur %s : %s", kiwi.get("name"), last_err)
        if not used:
            raise RuntimeError(last_err or "Aucun KiwiSDR n’a fourni d’audio")
        kiwi = used
        ch_out = {
            "id": "tx",
            "kind": "scan",
            "freq_khz": SCAN_20M_KHZ[0],
            "label": "Scan 20 m USB (14,19–14,275 MHz)",
            "zoom": 8,
            "screencast": True,
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
        meta["raw_results"] = [
            (repr(r) if isinstance(r, Exception) else r) for r in results
        ]
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
        _finalize_media(session_dir, meta)
        _write_meta(session_dir, meta)
        try:
            lock.unlink()
        except OSError:
            pass


HUNT_RECORD_S = 75.0
HUNT_RECORD_ZOOM = 10


async def run_test_hunt(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Chasse un QSO USB 14,150–14,260 kHz sur le waterfall, puis capture courte."""
    cfg = cfg or load_config()
    root = data_dir(cfg)
    lock = root / LOCK_NAME
    if lock.exists():
        raise RuntimeError("Un enregistrement est déjà en cours")
    lock.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
    started = datetime.now(timezone.utc)
    vid = started.strftime("%Y-%m-%dT%H%MZ") + "-hunt"
    session_dir = root / "vacations" / vid
    session_dir.mkdir(parents=True, exist_ok=True)
    viewport = (cfg.get("sdr") or {}).get("viewport") or {"width": 1280, "height": 800}
    ident = (cfg.get("sdr") or {}).get("ident_user") or "ggr-vacations"
    filt = (cfg.get("radio") or {}).get("usb_filter") or {}
    low_hz = int(filt.get("low_hz") or 300)
    high_hz = int(filt.get("high_hz") or 2700)
    meta: dict[str, Any] = {
        "id": vid,
        "status": "running",
        "reason": "test-hunt",
        "title": "Test chasse 20 m USB",
        "version": version(cfg),
        "club": cfg.get("club"),
        "started_at": started.isoformat(),
        "hunt_khz": [HUNT_LO_KHZ, HUNT_HI_KHZ],
        "channels": [],
    }
    _write_meta(session_dir, meta)
    try:
        fleet = await fetch_fleet(cfg)
        meta["fleet"] = fleet
        ranked = await fetch_ranked_kiwis(cfg, fleet["lat"], fleet["lon"])
        if not ranked:
            raise RuntimeError("Aucun KiwiSDR disponible pour la chasse 20 m")
        meta["kiwis_ranked"] = ranked[:8]
        hit = None
        used = None
        last_err = None
        for kiwi in ranked[:5]:
            try:
                hit = await hunt_usb_signal(
                    kiwi,
                    ident=ident,
                    low_hz=low_hz,
                    high_hz=high_hz,
                    lo_khz=HUNT_LO_KHZ,
                    hi_khz=HUNT_HI_KHZ,
                    cf_khz=HUNT_CF_KHZ,
                    zoom=HUNT_ZOOM,
                )
            except Exception as exc:
                last_err = str(exc)
                log.warning("Chasse %s : %s", kiwi.get("name"), exc)
                continue
            if hit:
                used = kiwi
                break
            last_err = f"pas de trafic {HUNT_LO_KHZ:.0f}–{HUNT_HI_KHZ:.0f} kHz sur {kiwi.get('name')}"
            log.info("%s", last_err)
        if not hit or not used:
            raise RuntimeError(last_err or "Aucun trafic USB détecté sur 14,150–14,260 kHz")
        freq = float(hit["freq_khz"])
        meta["hunt"] = {k: hit[k] for k in hit if k != "kiwi"}
        wav = session_dir / "audio-tx.wav"
        webm = session_dir / "screencast-tx.webm"
        overlay = {
            "when": started.strftime("%Y-%m-%d %H:%M"),
            "channel": "Chasse 20 m USB",
            "freq": f"{freq:.2f} kHz USB · {low_hz}–{high_hz} Hz",
            "kiwi": used.get("name"),
        }
        raw = await record_screencast(
            used,
            freq,
            webm,
            HUNT_RECORD_S,
            mode="usb",
            zoom=HUNT_RECORD_ZOOM,
            viewport=viewport,
            overlay=overlay,
            snd_wav=wav,
        )
        if not (isinstance(raw, dict) and raw.get("snd_packets")):
            raise RuntimeError(
                (raw.get("error") if isinstance(raw, dict) else str(raw)) or "capture sans audio"
            )
        ch_out = {
            "id": "tx",
            "kind": "hunt",
            "freq_khz": freq,
            "label": f"QSO {freq:.2f} kHz USB",
            "zoom": HUNT_RECORD_ZOOM,
            "screencast": True,
            "screencast_raw": "screencast-tx.webm",
            "kiwi": {
                "name": used.get("name"),
                "url": used.get("url"),
                "distance_km": used.get("distance_km"),
                "fmt": used.get("fmt"),
                "snr_hf": used.get("snr_hf"),
            },
        }
        if isinstance(raw.get("audio_delay_s"), (int, float)):
            ch_out["audio_delay_s"] = round(float(raw["audio_delay_s"]), 3)
        meta["channels"].append(ch_out)
        meta["raw_results"] = [raw]
        meta["status"] = "complete"
        meta["ended_at"] = datetime.now(timezone.utc).isoformat()
        meta["fleet_fmt"] = fleet.get("fmt") or fmt_latlon(fleet["lat"], fleet["lon"])
        log.info("Chasse 20 m %s @ %.2f kHz terminée", vid, freq)
        return meta
    except Exception as exc:
        meta["status"] = "error"
        meta["error"] = str(exc)
        meta["ended_at"] = datetime.now(timezone.utc).isoformat()
        log.exception("Chasse 20 m %s en échec", vid)
        return meta
    finally:
        _finalize_media(session_dir, meta)
        _write_meta(session_dir, meta)
        try:
            lock.unlink()
        except OSError:
            pass


def recover_orphaned(cfg: dict[str, Any] | None = None) -> int:
    """Au démarrage : lock et « running » orphelins — sans mux ffmpeg (trop long pour /health)."""
    cfg = cfg or load_config()
    root = data_dir(cfg)
    n = 0
    lock = root / LOCK_NAME
    if lock.exists():
        try:
            lock.unlink()
            n += 1
            log.warning("Verrou d'enregistrement orphelin retiré")
        except OSError:
            pass
    vac_root = root / "vacations"
    if not vac_root.exists():
        return n
    for folder in vac_root.iterdir():
        if not folder.is_dir():
            continue
        meta_path = folder / "metadata.json"
        if not meta_path.is_file():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if meta.get("status") != "running":
            continue
        meta["status"] = "error"
        meta["error"] = ORPHAN_ERROR
        meta["ended_at"] = datetime.now(timezone.utc).isoformat()
        _write_meta(folder, meta)
        n += 1
        log.warning("Vacation %s marquée interrompue", folder.name)
    return n


def finalize_pending_sessions(cfg: dict[str, Any] | None = None) -> int:
    """Mux WAV/WebM restants une fois l’UI déjà joignable."""
    cfg = cfg or load_config()
    vac_root = data_dir(cfg) / "vacations"
    if not vac_root.exists():
        return 0
    n = 0
    for folder in vac_root.iterdir():
        if not folder.is_dir():
            continue
        meta_path = folder / "metadata.json"
        if not meta_path.is_file():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        before = json.dumps(meta, sort_keys=True)
        _finalize_media(folder, meta)
        has_media = any(ch.get("video") or ch.get("audio") for ch in (meta.get("channels") or []))
        if meta.get("error") == ORPHAN_ERROR and has_media:
            meta["status"] = "complete"
            meta.pop("error", None)
            meta["ended_at"] = datetime.now(timezone.utc).isoformat()
            log.info("Vacation %s finalisée après interruption", folder.name)
        if json.dumps(meta, sort_keys=True) != before:
            _write_meta(folder, meta)
            n += 1
    return n


def _channel_audio_delay(session_dir: Path, ch: dict[str, Any]) -> float:
    raw = ch.get("audio_delay_s")
    if isinstance(raw, (int, float)):
        return max(0.0, float(raw))
    cid = ch.get("id") or "tx"
    sidecar = session_dir / f"audio-{cid}.delay"
    if sidecar.is_file():
        try:
            return max(0.0, float(sidecar.read_text(encoding="utf-8").strip()))
        except ValueError:
            return 0.0
    return 0.0


def _mux_channel(session_dir: Path, ch: dict[str, Any], raw: Path) -> None:
    wav = session_dir / (ch.get("audio_file") or ch.get("audio") or f"audio-{ch.get('id') or 'tx'}.wav")
    cid = ch.get("id") or "tx"
    mp4 = session_dir / f"screencast-{cid}.mp4"
    if mux_screencast(raw, wav, mp4, audio_delay_s=_channel_audio_delay(session_dir, ch)):
        ch["video"] = mp4.name
        thumb = session_dir / f"thumb-{cid}.jpg"
        at_s = 30 if cid == "tx" else 45
        if thumbnail(mp4, thumb, at_s=at_s):
            ch["thumb"] = thumb.name
        raw.unlink(missing_ok=True)


def _finalize_media(session_dir: Path, meta: dict[str, Any]) -> None:
    """Mux WAV/WebM restants, y compris un .webm Playwright au nom hashé."""
    channels = meta.setdefault("channels", [])
    for ch in channels:
        wav = session_dir / (ch.get("audio_file") or ch.get("audio") or f"audio-{ch['id']}.wav")
        if wav.is_file() and wav.stat().st_size > 64:
            ch["audio"] = wav.name
        else:
            ch.pop("audio", None)
        ch.pop("audio_file", None)
        raw_name = ch.get("screencast_raw")
        raw = session_dir / raw_name if raw_name else session_dir / f"screencast-{ch['id']}.webm"
        if raw.is_file() and raw.stat().st_size > 64:
            _mux_channel(session_dir, ch, raw)
        ch.pop("screencast_raw", None)

    leftovers = [p for p in session_dir.glob("*.webm") if p.is_file() and p.stat().st_size > 64]
    if not leftovers:
        return
    host = next((c for c in channels if c.get("id") == "tx" or c.get("screencast")), None)
    if host is None and channels:
        host = channels[0]
    if host is None:
        host = {
            "id": "tx",
            "kind": "tx",
            "freq_khz": 14135.0,
            "label": "Bulletin météo F6KUF",
            "screencast": True,
        }
        channels.insert(0, host)
    if not host.get("video"):
        _mux_channel(session_dir, host, max(leftovers, key=lambda p: p.stat().st_size))


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
    parser.add_argument("--buddy", action="store_true", help="Lancer un buddy call immédiat (4483 / 6516 kHz)")
    parser.add_argument("--test-20m", action="store_true", help="Scan USB 20 m (test), puis archive dans l’UI")
    parser.add_argument(
        "--test-hunt",
        action="store_true",
        help="Chasse un QSO USB 14,150–14,260 kHz sur le waterfall, capture 75 s",
    )
    parser.add_argument("--config", default=os.environ.get("GGR_CONFIG"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config(args.config)
    if args.test_hunt:
        meta = asyncio.run(run_test_hunt(cfg))
        print(json.dumps({"id": meta.get("id"), "status": meta.get("status"), "error": meta.get("error"), "hunt": meta.get("hunt")}, ensure_ascii=False))
        if meta.get("status") != "complete":
            raise SystemExit(1)
        return
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
    if args.buddy:
        meta = asyncio.run(run_buddy_call(cfg, reason="buddy-manual"))
        print(json.dumps({"id": meta.get("id"), "status": meta.get("status"), "error": meta.get("error")}, ensure_ascii=False))
        if meta.get("status") != "complete":
            raise SystemExit(1)
        return
    parser.print_help()


if __name__ == "__main__":
    main()
