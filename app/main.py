"""Application web GGR Vacations — catalogue et replay des vacations HF."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app import store
from recorder.config import ack_label, fmt_khz, fmt_mhz, load_config, parse_qrg_khz, qrg_context, save_runtime_settings, version
from recorder.fleet import buddy_aim, fetch_fleet
from recorder.kiwi_list import assign_buddy_kiwis, fetch_ranked_kiwis
from recorder.scheduler import apply_vacation_schedule, build_scheduler
from recorder.session import (
    finalize_pending_sessions,
    next_vacation_utc,
    recover_orphaned,
    run_buddy_call,
    run_manual_qrg,
    run_vacation,
)

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent
CFG = load_config()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log.info("Templates : %s → %s", ROOT / "templates", list((ROOT / "templates").glob("*.html")))
    cfg = load_config()
    recovered = recover_orphaned(cfg)
    if recovered:
        log.warning("Récupération : %s verrou(s) / vacation(s) orphelin(s)", recovered)
    scheduler = build_scheduler(cfg)
    _app.state.scheduler = scheduler
    scheduler.start()

    async def _mux_pending() -> None:
        try:
            n = await asyncio.to_thread(finalize_pending_sessions, load_config())
            if n:
                log.info("Finalisation média : %s session(s)", n)
        except Exception:
            log.exception("Finalisation média")

    mux_task = asyncio.create_task(_mux_pending())
    try:
        yield
    finally:
        mux_task.cancel()
        scheduler.shutdown(wait=False)


app = FastAPI(title="GGR Vacations", version=version(CFG), lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")
jinja = Environment(
    loader=FileSystemLoader(str(ROOT / "templates")),
    autoescape=select_autoescape(["html", "xml"]),
)
jinja.filters["when"] = store.iso_to_label


def render(request: Request, name: str, **extra) -> HTMLResponse:
    """Rendu Jinja direct — Starlette TemplateResponse passe parfois le context dict comme nom de template."""
    try:
        ctx = _ctx(request, **extra)
        html = jinja.get_template(name).render(**ctx)
        return HTMLResponse(html)
    except Exception as exc:
        log.exception("Rendu %s", name)
        return HTMLResponse(
            f"<!doctype html><pre>Erreur interne : {type(exc).__name__}: {exc}</pre>",
            status_code=500,
        )


def _ctx(request: Request, **extra):
    cfg = load_config()
    nxt = next_vacation_utc(cfg)
    bulletin = _bulletin_utc(cfg)
    buddy_at = _buddy_clock_utc(cfg)
    club = cfg.get("club") or {}
    schedule = cfg.get("schedule") or {}
    qrg = qrg_context(cfg)
    return {
        "app_name": (cfg.get("web") or {}).get("title") or "GGR Vacations",
        "version": version(cfg),
        "club": club,
        "club_callsign": club.get("callsign") or "F6KUF",
        "radio": cfg.get("radio") or {},
        "schedule": schedule,
        "schedule_lead": qrg["schedule_lead"],
        "next_start": nxt,
        "next_start_iso": nxt.isoformat(),
        "bulletin_iso": bulletin.isoformat(),
        "bulletin_label": bulletin.strftime("%d/%m %H:%M"),
        "buddy_iso": buddy_at.isoformat(),
        "buddy_label": buddy_at.strftime("%d/%m %H:%M"),
        "recording": store.recording_in_progress(cfg),
        "admin_configured": _admin_configured(cfg),
        **qrg,
        **extra,
    }


def _admin_configured(cfg: dict | None = None) -> bool:
    cfg = cfg or load_config()
    token = (cfg.get("web") or {}).get("admin_token") or os.environ.get("GGR_ADMIN_TOKEN") or ""
    return bool(token)


def _require_admin(x_admin_token: str | None, cfg: dict | None = None) -> None:
    cfg = cfg or load_config()
    expected = (cfg.get("web") or {}).get("admin_token") or os.environ.get("GGR_ADMIN_TOKEN") or ""
    if not expected or x_admin_token != expected:
        raise HTTPException(403, "Jeton administrateur invalide")


def _clock_utc(time_utc: str) -> datetime:
    hh, mm = (time_utc or "00:00").split(":")
    now = datetime.now(timezone.utc)
    t = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    if t <= now:
        from datetime import timedelta

        t = t + timedelta(days=1)
    return t


def _bulletin_utc(cfg) -> datetime:
    sched = cfg.get("schedule") or {}
    return _clock_utc(str(sched.get("time_utc") or "18:00"))


def _buddy_clock_utc(cfg) -> datetime:
    buddy = cfg.get("buddy") or {}
    return _clock_utc(str(buddy.get("time_utc") or "12:00"))


@app.get("/health")
async def health():
    cfg = load_config()
    return {"ok": True, "version": version(cfg), "recording": store.recording_in_progress(cfg)}


@app.get("/ping", response_class=HTMLResponse)
async def ping():
    return HTMLResponse("<!doctype html><p>ggr-vacations ping</p>")


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    vacations = store.list_vacations(load_config())
    return render(request, "index.html", vacations=vacations)


@app.get("/vacations/{vacation_id}", response_class=HTMLResponse)
async def vacation_page(request: Request, vacation_id: str):
    meta = store.get_vacation(vacation_id, load_config())
    if not meta:
        raise HTTPException(404, "Vacation introuvable")
    return render(request, "vacation.html", vacation=meta)


@app.get("/flotte", response_class=HTMLResponse)
async def flotte_page(request: Request):
    cfg = load_config()
    try:
        fleet = await fetch_fleet(cfg)
        aim = buddy_aim(fleet, cfg)
        try:
            kiwis = await fetch_ranked_kiwis(cfg, fleet["lat"], fleet["lon"], limit=8)
        except Exception:
            log.exception("Liste KiwiSDR indisponible")
            kiwis = []
        buddy_kiwis = []
        try:
            qrg = qrg_context(cfg)
            cover = [int(round(qrg["buddy_main_khz"] * 1000)), int(round(qrg["buddy_alt_khz"] * 1000))]
            pool = await fetch_ranked_kiwis(
                cfg,
                float(aim["lat"]),
                float(aim["lon"]),
                limit=0,
                min_free=1,
                cover_hz=cover,
                score_mode="buddy",
            )
            roles = assign_buddy_kiwis(pool, lat=float(aim["lat"]), lon=float(aim["lon"]), cfg=cfg)
            buddy_kiwis = list(roles.values())
        except Exception:
            log.exception("KiwiSDR buddy indisponibles")
    except Exception as exc:
        log.exception("Page flotte")
        return HTMLResponse(
            f"<!doctype html><pre>Erreur flotte : {type(exc).__name__}: {exc}</pre>",
            status_code=500,
        )
    return render(
        request,
        "flotte.html",
        fleet=fleet,
        kiwis=kiwis,
        buddy_aim=aim,
        buddy_kiwis=buddy_kiwis,
    )


@app.get("/a-propos", response_class=HTMLResponse)
async def about_page(request: Request):
    return render(request, "about.html")


@app.get("/reglages", response_class=HTMLResponse)
async def reglages_page(request: Request):
    cfg = load_config()
    boats = []
    try:
        fleet = await fetch_fleet(cfg)
        boats = fleet.get("boats") or []
    except Exception:
        log.exception("Skippers indisponibles pour Réglages")
    return render(request, "reglages.html", boats=boats)


@app.get("/media/{vacation_id}/{filename}")
async def media(vacation_id: str, filename: str):
    path = store.media_path(vacation_id, filename, load_config())
    if not path:
        raise HTTPException(404)
    return FileResponse(path)


@app.get("/api/vacations")
async def api_vacations():
    return store.list_vacations(load_config())


@app.get("/api/vacations/{vacation_id}")
async def api_vacation(vacation_id: str):
    meta = store.get_vacation(vacation_id, load_config())
    if not meta:
        raise HTTPException(404)
    meta.pop("_dir", None)
    return meta


@app.delete("/api/vacations/{vacation_id}")
async def api_vacation_delete(
    vacation_id: str,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
):
    cfg = load_config()
    _require_admin(x_admin_token, cfg)
    err = store.delete_vacation(vacation_id, cfg)
    if err == "introuvable":
        raise HTTPException(404, "Vacation introuvable")
    if err == "enregistrement en cours":
        raise HTTPException(409, "Vacation en cours d’enregistrement")
    if err:
        raise HTTPException(400, err)
    return {"ok": True, "deleted": vacation_id}


@app.post("/api/vacations/{vacation_id}/delete")
async def api_vacation_delete_post(
    vacation_id: str,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
):
    """Alias POST : certains proxys bloquent DELETE."""
    return await api_vacation_delete(vacation_id, x_admin_token)


@app.get("/api/settings")
async def api_settings_get():
    cfg = load_config()
    qrg = qrg_context(cfg)
    qrg["admin_configured"] = _admin_configured(cfg)
    qrg["recording"] = store.recording_in_progress(cfg)
    return qrg


def _khz_field(body: dict, key: str, label: str) -> float:
    try:
        khz = round(float(body[key]), 4)
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(400, f"{label} invalide") from exc
    if not (1000.0 <= khz <= 30000.0):
        raise HTTPException(400, f"{label} hors bande HF (1000–30000 kHz)")
    return khz


@app.put("/api/settings")
async def api_settings_put(
    request: Request,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
):
    cfg = load_config()
    _require_admin(x_admin_token, cfg)
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(400, "JSON invalide") from exc
    if not isinstance(body, dict):
        raise HTTPException(400, "JSON objet attendu")
    tx_khz = _khz_field(body, "tx_khz", "QRG TX")
    ack1_khz = _khz_field(body, "ack1_khz", "QRG ACK 16 m")
    ack2_khz = _khz_field(body, "ack2_khz", "QRG ACK 12 m")
    try:
        tol = round(float(body.get("qrg_tolerance_khz", 5.0)), 3)
        lead = int(body.get("lead_minutes", 1))
        duration = int(body.get("duration_minutes", 10))
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, "Tolérance, avance ou durée invalide") from exc
    if not (0.1 <= tol <= 15.0):
        raise HTTPException(400, "Tolérance QRG hors plage (0,1–15 kHz)")
    if not (0 <= lead <= 15):
        raise HTTPException(400, "Avance hors plage (0–15 min)")
    if not (1 <= duration <= 45):
        raise HTTPException(400, "Durée hors plage (1–45 min)")
    radio = cfg.get("radio") or {}
    acks = [dict(row) for row in (radio.get("ack") or [])]
    while len(acks) < 2:
        acks.append({})
    acks[0]["freq_khz"] = ack1_khz
    acks[0]["label"] = ack_label(ack1_khz)
    acks[1]["freq_khz"] = ack2_khz
    acks[1]["label"] = ack_label(ack2_khz)
    patch = {
        "radio": {
            "tx": {"freq_khz": tx_khz, "qrg_tolerance_khz": tol},
            "ack": acks[:2],
        },
        "schedule": {"lead_minutes": lead, "duration_minutes": duration},
    }
    if "buddy_main_khz" in body or "buddy_skippers" in body:
        buddy_cfg = dict(cfg.get("buddy") or {})
        main = dict(buddy_cfg.get("main") or {})
        alt = dict(buddy_cfg.get("alternate") or {})
        cent = dict(buddy_cfg.get("centroid") or {})
        kiwi = dict(buddy_cfg.get("kiwi") or {})
        if "buddy_main_khz" in body:
            main["freq_khz"] = _khz_field(body, "buddy_main_khz", "QRG buddy 4483")
            main["label"] = f"Buddy call {fmt_khz(main['freq_khz'])} kHz"
        if "buddy_alt_khz" in body:
            alt["freq_khz"] = _khz_field(body, "buddy_alt_khz", "QRG buddy 6516")
            alt["label"] = f"Buddy call {fmt_khz(alt['freq_khz'])} kHz (secours)"
        if "buddy_time_utc" in body:
            from recorder.config import _hhmm

            buddy_cfg["time_utc"] = _hhmm(str(body.get("buddy_time_utc") or ""), "12:00")
        if "buddy_lead" in body:
            try:
                b_lead = int(body.get("buddy_lead"))
            except (TypeError, ValueError) as exc:
                raise HTTPException(400, "Avance buddy invalide") from exc
            if not (0 <= b_lead <= 15):
                raise HTTPException(400, "Avance buddy hors plage (0–15 min)")
            buddy_cfg["lead_minutes"] = b_lead
        if "buddy_duration_minutes" in body:
            try:
                b_dur = int(body.get("buddy_duration_minutes"))
            except (TypeError, ValueError) as exc:
                raise HTTPException(400, "Durée buddy invalide") from exc
            if not (1 <= b_dur <= 45):
                raise HTTPException(400, "Durée buddy hors plage (1–45 min)")
            buddy_cfg["duration_minutes"] = b_dur
        if "buddy_enabled" in body:
            buddy_cfg["enabled"] = bool(body.get("buddy_enabled"))
        if "buddy_include_fleet" in body:
            cent["include_fleet"] = bool(body.get("buddy_include_fleet"))
        if "buddy_skippers" in body:
            raw_skip = body.get("buddy_skippers")
            if isinstance(raw_skip, str):
                names = [ln.strip() for ln in raw_skip.splitlines() if ln.strip()]
            elif isinstance(raw_skip, list):
                names = [str(x).strip() for x in raw_skip if str(x).strip()]
            else:
                raise HTTPException(400, "Liste de skippers buddy invalide")
            cent["skippers"] = names
        if "buddy_kiwi_count" in body:
            try:
                n_kiwi = int(body.get("buddy_kiwi_count"))
            except (TypeError, ValueError) as exc:
                raise HTTPException(400, "Nombre de Kiwi buddy invalide") from exc
            if not (1 <= n_kiwi <= 8):
                raise HTTPException(400, "Nombre de Kiwi buddy hors plage (1–8)")
            kiwi["count"] = n_kiwi
        buddy_cfg["main"] = main
        buddy_cfg["alternate"] = alt
        buddy_cfg["centroid"] = cent
        buddy_cfg["kiwi"] = kiwi
        patch["buddy"] = buddy_cfg
    new_cfg = save_runtime_settings(patch, cfg)
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is not None:
        apply_vacation_schedule(scheduler, new_cfg)
    qrg = qrg_context(new_cfg)
    qrg["ok"] = True
    qrg["admin_configured"] = _admin_configured(new_cfg)
    return qrg


@app.post("/api/vacations/record")
async def api_record(
    request: Request,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
):
    cfg = load_config()
    _require_admin(x_admin_token, cfg)
    if store.recording_in_progress(cfg):
        raise HTTPException(409, "Enregistrement déjà en cours")
    duration = None
    freq_khz = None
    hunt = True
    tol = None
    kind = "vacation"
    raw = await request.body()
    if raw:
        try:
            body = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HTTPException(400, "JSON invalide") from exc
        if isinstance(body, dict):
            kind = str(body.get("kind") or "vacation")
            if body.get("duration_minutes") is not None:
                try:
                    duration = int(body["duration_minutes"])
                except (TypeError, ValueError) as exc:
                    raise HTTPException(400, "duration_minutes invalide") from exc
                if duration < 1 or duration > 45:
                    raise HTTPException(400, "Durée hors plage (1–45 min)")
            if body.get("freq_khz") is not None or body.get("freq") is not None:
                try:
                    freq_khz = parse_qrg_khz(body.get("freq_khz", body.get("freq")))
                except ValueError as exc:
                    raise HTTPException(400, str(exc)) from exc
            if "hunt" in body:
                hunt = bool(body.get("hunt"))
            if body.get("qrg_tolerance_khz") is not None:
                try:
                    tol = float(body["qrg_tolerance_khz"])
                except (TypeError, ValueError) as exc:
                    raise HTTPException(400, "Tolérance QRG invalide") from exc
                if not (0.1 <= tol <= 15.0):
                    raise HTTPException(400, "Tolérance QRG hors plage (0,1–15 kHz)")
    if freq_khz is not None:
        minutes = duration if duration is not None else 2
        asyncio.create_task(
            run_manual_qrg(
                freq_khz=freq_khz,
                duration_minutes=minutes,
                hunt=hunt,
                qrg_tolerance_khz=tol,
            )
        )
        return JSONResponse(
            {
                "ok": True,
                "status": "started",
                "mode": "qrg",
                "freq_khz": freq_khz,
                "freq_mhz": fmt_mhz(freq_khz),
                "duration_minutes": minutes,
                "hunt": hunt,
            },
            status_code=202,
        )
    if kind == "buddy":
        minutes = duration if duration is not None else int((cfg.get("buddy") or {}).get("duration_minutes") or 15)
        asyncio.create_task(run_buddy_call(reason="buddy-api", duration_minutes=minutes))
        return JSONResponse(
            {"ok": True, "status": "started", "mode": "buddy", "duration_minutes": minutes},
            status_code=202,
        )
    asyncio.create_task(run_vacation(reason="api", duration_minutes=duration))
    minutes = duration if duration is not None else int((cfg.get("schedule") or {}).get("duration_minutes") or 10)
    return JSONResponse(
        {"ok": True, "status": "started", "mode": "vacation", "duration_minutes": minutes},
        status_code=202,
    )


def run() -> None:
    import uvicorn

    web = CFG.get("web") or {}
    uvicorn.run(
        "app.main:app",
        host=web.get("host") or "0.0.0.0",
        port=int(web.get("port") or 8080),
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    run()
