"""Chargement de la configuration YAML (défaut + surcharge)."""

from __future__ import annotations

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
    return cfg


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
        return str(cfg.get("version") or "0.1.5")
