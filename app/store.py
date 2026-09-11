"""Catalogue des vacations enregistrées (un dossier + metadata.json)."""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from recorder.config import data_dir, load_config

_VACATION_ID = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]{0,80}$")


def vacations_root(cfg: dict[str, Any] | None = None) -> Path:
    return data_dir(cfg) / "vacations"


def _decorate(meta: dict[str, Any]) -> dict[str, Any]:
    thumb = None
    tx = None
    for ch in meta.get("channels") or []:
        if ch.get("id") == "tx":
            tx = ch
        if ch.get("thumb") and not thumb:
            thumb = ch["thumb"]
    meta["thumb"] = thumb
    meta["tx"] = tx
    meta["is_test"] = meta.get("reason") in ("test-20m", "test-hunt", "manual-qrg")
    return meta


def list_vacations(cfg: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    root = vacations_root(cfg)
    if not root.exists():
        return []
    items: list[dict[str, Any]] = []
    for folder in sorted(root.iterdir(), reverse=True):
        meta_path = folder / "metadata.json"
        if not meta_path.is_file():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        meta["id"] = meta.get("id") or folder.name
        items.append(_decorate(meta))
    return items


def get_vacation(vacation_id: str, cfg: dict[str, Any] | None = None) -> dict[str, Any] | None:
    folder = vacations_root(cfg) / vacation_id
    meta_path = folder / "metadata.json"
    if not meta_path.is_file():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    meta["id"] = meta.get("id") or vacation_id
    meta["_dir"] = str(folder)
    return _decorate(meta)


def media_path(vacation_id: str, filename: str, cfg: dict[str, Any] | None = None) -> Path | None:
    if "/" in filename or filename.startswith("."):
        return None
    path = vacations_root(cfg) / vacation_id / filename
    if not path.is_file():
        return None
    return path


def recording_in_progress(cfg: dict[str, Any] | None = None) -> bool:
    return (data_dir(cfg) / ".recording.lock").exists()


def delete_vacation(vacation_id: str, cfg: dict[str, Any] | None = None) -> str | None:
    """Supprime le dossier d’une vacation. None = ok, sinon motif d’échec."""
    if not _VACATION_ID.match(vacation_id or "") or ".." in vacation_id:
        return "identifiant invalide"
    folder = vacations_root(cfg) / vacation_id
    if not folder.is_dir() or not (folder / "metadata.json").is_file():
        return "introuvable"
    meta = get_vacation(vacation_id, cfg)
    if meta and meta.get("status") == "running" and recording_in_progress(cfg):
        return "enregistrement en cours"
    shutil.rmtree(folder)
    return None


def iso_to_label(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%d/%m/%Y %H:%M TU")
    except ValueError:
        return iso


def default_cfg() -> dict[str, Any]:
    return load_config()
