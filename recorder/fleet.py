"""Position moyenne de la flotte GGR via Yellowbrick (binaire Positions3)."""

from __future__ import annotations

import logging
import struct
import unicodedata
from datetime import datetime, timezone
from typing import Any

from recorder.geo import centroid, fmt_latlon, haversine_km, initial_bearing

log = logging.getLogger(__name__)

# Identifiants historiques / « fantômes » sur le tracker GGR 2026 (RKJ, Moitessier, etc.)
GHOST_TEAM_ID_FROM = 900


def parse_positions3(buf: bytes) -> list[dict[str, Any]]:
    """Décode le binaire Yellowbrick Positions3 (AllPositions3 / LatestPositions3).

    Format public du viewer YB : drapeau, epoch, puis par bateau une liste de
    « moments » en coordonnées × 1e5. Implémentation originale d'après le
    protocole du viewer (DataView big-endian), pas une copie du JS minifié.
    """
    if len(buf) < 5:
        return []
    flags = buf[0]
    has_alt = bool(flags & 1)
    has_dtf = bool(flags & 2)
    has_lap = bool(flags & 4)
    has_pc = bool(flags & 8)
    epoch = struct.unpack(">I", buf[1:5])[0]
    offset = 5
    teams: list[dict[str, Any]] = []
    end = len(buf)
    while offset + 4 <= end:
        team_id = struct.unpack(">H", buf[offset : offset + 2])[0]
        offset += 2
        n_moments = struct.unpack(">H", buf[offset : offset + 2])[0]
        offset += 2
        moments: list[dict[str, Any]] = []
        prev: dict[str, Any] | None = None
        for _ in range(n_moments):
            if offset >= end:
                break
            first = buf[offset]
            moment: dict[str, Any] = {}
            if first & 128:
                if prev is None:
                    break
                packed = struct.unpack(">H", buf[offset : offset + 2])[0]
                offset += 2
                dlat = struct.unpack(">h", buf[offset : offset + 2])[0]
                offset += 2
                dlon = struct.unpack(">h", buf[offset : offset + 2])[0]
                offset += 2
                if has_alt:
                    moment["alt"] = struct.unpack(">h", buf[offset : offset + 2])[0]
                    offset += 2
                if has_dtf:
                    dtf_delta = struct.unpack(">h", buf[offset : offset + 2])[0]
                    offset += 2
                    moment["dtf"] = prev.get("dtf", 0) + dtf_delta
                    if has_lap:
                        moment["lap"] = buf[offset]
                        offset += 1
                if has_pc:
                    pc_delta = struct.unpack(">h", buf[offset : offset + 2])[0] / 32000.0
                    offset += 2
                    moment["pc"] = prev.get("pc", 0.0) + pc_delta
                moment["lat"] = prev["lat"] + dlat
                moment["lon"] = prev["lon"] + dlon
                moment["at"] = prev["at"] - (packed & 32767)
            else:
                dt = struct.unpack(">I", buf[offset : offset + 4])[0]
                offset += 4
                lat_i = struct.unpack(">i", buf[offset : offset + 4])[0]
                offset += 4
                lon_i = struct.unpack(">i", buf[offset : offset + 4])[0]
                offset += 4
                if has_alt:
                    moment["alt"] = struct.unpack(">h", buf[offset : offset + 2])[0]
                    offset += 2
                if has_dtf:
                    moment["dtf"] = struct.unpack(">i", buf[offset : offset + 4])[0]
                    offset += 4
                    if has_lap:
                        moment["lap"] = buf[offset]
                        offset += 1
                if has_pc:
                    moment["pc"] = struct.unpack(">i", buf[offset : offset + 4])[0] / 21000000.0
                    offset += 4
                moment["lat"] = lat_i
                moment["lon"] = lon_i
                moment["at"] = epoch + dt
            moments.append(moment)
            prev = moment
        for moment in moments:
            moment["lat"] = moment["lat"] / 1e5
            moment["lon"] = moment["lon"] / 1e5
        teams.append({"id": team_id, "moments": moments})
    return teams


def _is_racing_team(team: dict[str, Any], skip_from: int) -> bool:
    if int(team.get("id") or 0) >= skip_from:
        return False
    status = str(team.get("status") or "").upper()
    return status in ("", "RACING")


def _latest_fix(moments: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not moments:
        return None
    return max(moments, key=lambda m: m.get("at") or 0)


def _team_colour(meta: dict[str, Any]) -> str:
    raw = str(meta.get("colour") or "c9a227").strip().lstrip("#")
    if len(raw) in (3, 6) and all(c in "0123456789abcdefABCDEF" for c in raw):
        return f"#{raw}"
    return "#c9a227"


def _heading_deg(moments: list[dict[str, Any]]) -> float | None:
    """Cap d’après les deux derniers points distincts (≥ 50 m)."""
    ordered = sorted(moments, key=lambda m: m.get("at") or 0)
    if len(ordered) < 2:
        return None
    last = ordered[-1]
    for prev in reversed(ordered[:-1]):
        if haversine_km(prev["lat"], prev["lon"], last["lat"], last["lon"]) >= 0.05:
            return round(initial_bearing(prev["lat"], prev["lon"], last["lat"], last["lon"]), 1)
    return None


def _track_tail(moments: list[dict[str, Any]], limit: int = 36) -> list[list[float]]:
    ordered = sorted(moments, key=lambda m: m.get("at") or 0)[-limit:]
    return [[float(m["lat"]), float(m["lon"])] for m in ordered]


async def fetch_fleet(cfg: dict[str, Any], client: Any | None = None) -> dict[str, Any]:
    """Retourne le centroïde de la flotte en course, avec repli configuré."""
    import httpx

    fleet_cfg = cfg.get("fleet") or {}
    skip_from = int(fleet_cfg.get("skip_team_id_from") or GHOST_TEAM_ID_FROM)
    fallback = fleet_cfg.get("fallback") or {}
    result: dict[str, Any] = {
        "source": "fallback",
        "lat": float(fallback.get("lat", 46.5025)),
        "lon": float(fallback.get("lon", -1.7888)),
        "label": fallback.get("label") or "Position de repli",
        "n_boats": 0,
        "boats": [],
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }

    if str(fleet_cfg.get("source") or "") != "yellowbrick":
        result["fmt"] = fmt_latlon(result["lat"], result["lon"])
        return result

    race_id = fleet_cfg.get("race_id") or "ggr2026"
    host = fleet_cfg.get("tracker_host") or "cf.yb.tl"
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=30.0, headers={"User-Agent": "ggr-vacations/0.1"})
    try:
        setup_url = f"https://yb.tl/JSON/{race_id}/RaceSetup"
        pos_url = f"https://{host}/BIN/{race_id}/AllPositions3"
        setup_resp, pos_resp = await client.get(setup_url), await client.get(pos_url)
        setup_resp.raise_for_status()
        pos_resp.raise_for_status()
        setup = setup_resp.json()
        teams_meta = {int(t["id"]): t for t in setup.get("teams") or [] if "id" in t}
        parsed = parse_positions3(pos_resp.content)
        boats: list[dict[str, Any]] = []
        points: list[tuple[float, float]] = []
        for team in parsed:
            tid = int(team["id"])
            meta = teams_meta.get(tid) or {"id": tid, "name": f"Bateau {tid}", "status": "RACING"}
            if not _is_racing_team(meta, skip_from):
                continue
            fix = _latest_fix(team.get("moments") or [])
            if not fix:
                continue
            boats.append(
                {
                    "id": tid,
                    "name": meta.get("name"),
                    "sail": meta.get("sail"),
                    "status": meta.get("status"),
                    "colour": _team_colour(meta),
                    "lat": fix["lat"],
                    "lon": fix["lon"],
                    "heading": _heading_deg(team.get("moments") or []),
                    "track": _track_tail(team.get("moments") or []),
                    "at": fix.get("at"),
                }
            )
            points.append((fix["lat"], fix["lon"]))
        center = centroid(points)
        if center:
            result.update(
                {
                    "source": "yellowbrick",
                    "lat": center[0],
                    "lon": center[1],
                    "label": f"Centroïde flotte GGR ({len(points)} bateaux)",
                    "n_boats": len(points),
                    "boats": boats,
                    "race_id": race_id,
                }
            )
        else:
            result["warning"] = "Tracker joignable mais aucune position récente"
            log.warning("Flotte GGR : aucune position exploitable, repli utilisé")
    except Exception as exc:
        result["warning"] = f"Tracker indisponible : {exc}"
        log.warning("Flotte GGR : %s", exc)
    finally:
        if owns_client:
            await client.aclose()
    result["fmt"] = fmt_latlon(result["lat"], result["lon"])
    return result


def _fold_name(text: str) -> str:
    """Minuscules, sans accents, espaces normalisés — pour matcher les skippers."""
    nfkd = unicodedata.normalize("NFKD", text or "")
    stripped = "".join(ch for ch in nfkd if not unicodedata.combining(ch))
    return " ".join(stripped.lower().split())


def skipper_matches(boat: dict[str, Any], names: list[str], team_ids: list[int]) -> bool:
    if team_ids and int(boat.get("id") or 0) in team_ids:
        return True
    boat_name = _fold_name(str(boat.get("name") or ""))
    if not boat_name:
        return False
    for raw in names:
        needle = _fold_name(str(raw))
        if needle and (needle == boat_name or needle in boat_name or boat_name in needle):
            return True
    return False


def buddy_aim(fleet: dict[str, Any], cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Centroïde d’écoute buddy : trio (ou liste) avec ou sans le reste de la flotte."""
    cfg = cfg or {}
    buddy = cfg.get("buddy") or {}
    cent = buddy.get("centroid") or {}
    names = [str(x).strip() for x in (cent.get("skippers") or []) if str(x).strip()]
    try:
        team_ids = [int(x) for x in (cent.get("team_ids") or [])]
    except (TypeError, ValueError):
        team_ids = []
    include_fleet = bool(cent.get("include_fleet"))
    boats = [b for b in (fleet.get("boats") or []) if isinstance(b, dict)]
    core = [b for b in boats if skipper_matches(b, names, team_ids)]
    chosen = boats if include_fleet else core
    points = [
        (float(b["lat"]), float(b["lon"]))
        for b in chosen
        if b.get("lat") is not None and b.get("lon") is not None
    ]
    center = centroid(points) if points else None
    core_names = [str(b.get("name") or "?").strip() for b in core]
    if center:
        if include_fleet:
            label = f"Centroïde flotte ({len(points)} bateaux"
            if core_names:
                label += f", dont {', '.join(core_names)}"
            label += ")"
        else:
            label = "Centroïde buddy : " + (", ".join(core_names) or "aucun skipper")
        return {
            "lat": center[0],
            "lon": center[1],
            "fmt": fmt_latlon(center[0], center[1]),
            "label": label,
            "n_boats": len(points),
            "skippers": core,
            "skipper_names": core_names,
            "include_fleet": include_fleet,
            "source": fleet.get("source") or "buddy",
        }
    return {
        "lat": float(fleet.get("lat") or 46.5025),
        "lon": float(fleet.get("lon") or -1.7888),
        "fmt": fleet.get("fmt") or fmt_latlon(float(fleet.get("lat") or 46.5025), float(fleet.get("lon") or -1.7888)),
        "label": (fleet.get("label") or "Flotte") + " (repli buddy)",
        "n_boats": int(fleet.get("n_boats") or 0),
        "skippers": core,
        "skipper_names": core_names,
        "include_fleet": include_fleet,
        "source": fleet.get("source") or "fallback",
        "warning": "Skippers buddy introuvables — centroïde flotte utilisé",
    }
