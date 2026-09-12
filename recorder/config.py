"""Chargement de la configuration YAML (défaut + surcharge)."""

from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

from importlib.metadata import PackageNotFoundError, version as pkg_version

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config" / "default.yaml"


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """Charge config/default.yaml puis une éventuelle surcharge GGR_CONFIG."""
    with DEFAULT_CONFIG.open(encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    override_path = Path(path or os.environ.get("GGR_CONFIG", "/config/config.yaml"))
    if override_path.is_file() and override_path.resolve() != DEFAULT_CONFIG.resolve():
        with override_path.open(encoding="utf-8") as fh:
            extra = yaml.safe_load(fh) or {}
        if extra:
            cfg = _deep_merge(cfg, extra)
    data_dir = os.environ.get("GGR_DATA_DIR")
    if data_dir:
        cfg.setdefault("storage", {})["data_dir"] = data_dir
    token = os.environ.get("GGR_ADMIN_TOKEN")
    if token:
        cfg.setdefault("web", {})["admin_token"] = token
    settings = _settings_file(cfg)
    if settings.is_file():
        try:
            extra = json.loads(settings.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            extra = None
        if isinstance(extra, dict) and extra:
            cfg = _deep_merge(cfg, extra)
    if _migrate_legacy_ack_qrg(cfg):
        _write_settings_merge(cfg, {"radio": {"ack": list((cfg.get("radio") or {}).get("ack") or [])}})
    return cfg


def _settings_file(cfg: dict[str, Any]) -> Path:
    raw = os.environ.get("GGR_DATA_DIR") or (cfg.get("storage") or {}).get("data_dir") or "data"
    path = Path(raw)
    if not path.is_absolute():
        path = ROOT / path
    return path / "settings.json"


def _write_settings_merge(cfg: dict[str, Any], patch: dict[str, Any]) -> None:
    """Fusionne un correctif dans settings.json sans recharger la config (évite la récursion)."""
    path = _settings_file(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    current: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                current = loaded
        except (OSError, json.JSONDecodeError):
            current = {}
    merged = _deep_merge(current, patch)
    path.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# Anciennes QRG : le « 5 » final était la tolérance ± 5 kHz, pas un demi-kilohertz.
_LEGACY_ACK_KHZ = {16551.5: 16551.0, 12418.5: 12418.0}


def ack_label(freq_khz: float) -> str:
    return f"Accusé {fmt_mhz(freq_khz).replace('.', ',')} MHz"


def _migrate_legacy_ack_qrg(cfg: dict[str, Any]) -> bool:
    radio = cfg.get("radio")
    if not isinstance(radio, dict):
        return False
    acks = radio.get("ack")
    if not isinstance(acks, list):
        return False
    changed = False
    for row in acks:
        if not isinstance(row, dict):
            continue
        try:
            khz = round(float(row.get("freq_khz")), 4)
        except (TypeError, ValueError):
            continue
        new_khz = _LEGACY_ACK_KHZ.get(khz)
        if new_khz is None:
            continue
        row["freq_khz"] = new_khz
        row["label"] = ack_label(new_khz)
        changed = True
    return changed


def save_runtime_settings(patch: dict[str, Any], cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Écrit un correctif (QRG, durée…) dans data/settings.json, sans toucher au ConfigMap."""
    cfg = cfg or load_config()
    _write_settings_merge(cfg, patch)
    return load_config()


def parse_qrg_khz(value: float | int | str) -> float:
    """14.135 → 14135 kHz ; 14135 → 14135 kHz ; 16.551 → 16551 kHz."""
    try:
        raw = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("QRG invalide") from exc
    if 1.5 <= raw <= 30.0:
        khz = round(raw * 1000.0, 4)
    else:
        khz = round(raw, 4)
    if not (1000.0 <= khz <= 30000.0):
        raise ValueError("QRG hors bande HF (1,5–30 MHz ou 1000–30000 kHz)")
    return khz


def fmt_mhz(freq_khz: float) -> str:
    """14135.0 → 14.135 ; 16551.0 → 16.551."""
    text = f"{float(freq_khz) / 1000.0:.4f}".rstrip("0").rstrip(".")
    return text or "0"


def qrg_context(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """QRG / horaire exposés à l’UI (valeurs courantes, y compris settings.json)."""
    cfg = cfg or load_config()
    radio = cfg.get("radio") or {}
    tx = radio.get("tx") or {}
    acks = list(radio.get("ack") or [])
    sched = cfg.get("schedule") or {}
    tx_khz = float(tx.get("freq_khz") or 14135.0)
    ack1 = float(acks[0]["freq_khz"]) if acks else 16551.0
    ack2 = float(acks[1]["freq_khz"]) if len(acks) > 1 else 12418.0
    tol = float(tx.get("qrg_tolerance_khz") or 5.0)
    lead = int(sched.get("lead_minutes") or 1)
    duration = int(sched.get("duration_minutes") or 10)
    time_utc = str(sched.get("time_utc") or "18:00")
    ctx = {
        "tx_khz": tx_khz,
        "ack1_khz": ack1,
        "ack2_khz": ack2,
        "tx_mhz": fmt_mhz(tx_khz),
        "ack1_mhz": fmt_mhz(ack1),
        "ack2_mhz": fmt_mhz(ack2),
        "qrg_tolerance_khz": tol,
        "schedule_lead": lead,
        "duration_minutes": duration,
        "time_utc": time_utc,
        "tx_label": tx.get("label") or "Bulletin météo F6KUF",
        "ack1_label": (acks[0].get("label") if acks else None) or ack_label(ack1),
        "ack2_label": (acks[1].get("label") if len(acks) > 1 else None) or ack_label(ack2),
    }
    ctx.update(buddy_context(cfg))
    return ctx


def fmt_khz(freq_khz: float) -> str:
    """4483.0 → 4483 ; 6516.5 → 6516.5."""
    text = f"{float(freq_khz):.3f}".rstrip("0").rstrip(".")
    return text or "0"


def _hhmm(raw: str | None, default: str) -> str:
    text = str(raw or default).strip()
    parts = text.split(":")
    if len(parts) < 2:
        return default
    try:
        hh = int(parts[0])
        mm = int(parts[1])
    except ValueError:
        return default
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return default
    return f"{hh:02d}:{mm:02d}"


def buddy_context(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Buddy call 12:00 TU — QRG et centroïde exposés à l’UI."""
    cfg = cfg or load_config()
    buddy = cfg.get("buddy") or {}
    main = buddy.get("main") or {}
    alt = buddy.get("alternate") or {}
    cent = buddy.get("centroid") or {}
    kiwi = buddy.get("kiwi") or {}
    main_khz = float(main.get("freq_khz") or 4483.0)
    alt_khz = float(alt.get("freq_khz") or 6516.0)
    skippers = [str(x).strip() for x in (cent.get("skippers") or []) if str(x).strip()]
    return {
        "buddy_enabled": bool(buddy.get("enabled", True)),
        "buddy_time_utc": _hhmm(buddy.get("time_utc"), "12:00"),
        "buddy_lead": int(buddy.get("lead_minutes") or 1),
        "buddy_duration_minutes": int(buddy.get("duration_minutes") or 15),
        "buddy_main_khz": main_khz,
        "buddy_alt_khz": alt_khz,
        "buddy_main_label": main.get("label") or f"Buddy call {fmt_khz(main_khz)} kHz",
        "buddy_alt_label": alt.get("label") or f"Buddy call {fmt_khz(alt_khz)} kHz (secours)",
        "buddy_skippers": skippers,
        "buddy_include_fleet": bool(cent.get("include_fleet")),
        "buddy_kiwi_count": int(kiwi.get("count") or 4),
        "buddy_skippers_short": ", ".join(skippers) if skippers else "flotte",
    }


def data_dir(cfg: dict[str, Any] | None = None) -> Path:
    cfg = cfg or load_config()
    raw = cfg.get("storage", {}).get("data_dir") or "data"
    path = Path(raw)
    if not path.is_absolute():
        path = ROOT / path
    path.mkdir(parents=True, exist_ok=True)
    return path


def version(cfg: dict[str, Any] | None = None) -> str:
    """Version affichée = paquet installé (pyproject), pas le ConfigMap k3s éventuellement périmé."""
    try:
        return pkg_version("ggr-vacations")
    except PackageNotFoundError:
        cfg = cfg or {}
        return str(cfg.get("version") or "0.2.0")
