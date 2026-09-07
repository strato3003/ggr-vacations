"""Application web GGR Vacations — catalogue et replay des vacations HF."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import store
from recorder.config import load_config, version
from recorder.fleet import fetch_fleet
from recorder.kiwi_list import fetch_ranked_kiwis
from recorder.scheduler import build_scheduler
from recorder.session import next_vacation_utc, run_vacation

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent
CFG = load_config()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    scheduler = build_scheduler(CFG)
    scheduler.start()
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)


app = FastAPI(title="GGR Vacations", version=version(CFG), lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
templates = Jinja2Templates(directory=str(ROOT / "templates"))
templates.env.filters["when"] = store.iso_to_label


def _ctx(request: Request, **extra):
    nxt = next_vacation_utc(CFG)
    bulletin = _bulletin_utc(CFG)
    return {
        "request": request,
        "app_name": (CFG.get("web") or {}).get("title") or "GGR Vacations",
        "version": version(CFG),
        "club": CFG.get("club") or {},
        "radio": CFG.get("radio") or {},
        "schedule": CFG.get("schedule") or {},
        "next_start": nxt,
        "next_start_iso": nxt.isoformat(),
        "bulletin_utc": bulletin,
        "recording": store.recording_in_progress(CFG),
        **extra,
    }


def _bulletin_utc(cfg) -> datetime:
    sched = cfg.get("schedule") or {}
    hh, mm = (sched.get("time_utc") or "18:00").split(":")
    now = datetime.now(timezone.utc)
    t = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    if t <= now:
        from datetime import timedelta

        t = t + timedelta(days=1)
    return t


@app.get("/health")
async def health():
    return {"ok": True, "version": version(CFG), "recording": store.recording_in_progress(CFG)}


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    vacations = store.list_vacations(CFG)
    return templates.TemplateResponse("index.html", _ctx(request, vacations=vacations))


@app.get("/vacations/{vacation_id}", response_class=HTMLResponse)
async def vacation_page(request: Request, vacation_id: str):
    meta = store.get_vacation(vacation_id, CFG)
    if not meta:
        raise HTTPException(404, "Vacation introuvable")
    return templates.TemplateResponse("vacation.html", _ctx(request, vacation=meta))


@app.get("/flotte", response_class=HTMLResponse)
async def flotte_page(request: Request):
    fleet = await fetch_fleet(CFG)
    kiwis = await fetch_ranked_kiwis(CFG, fleet["lat"], fleet["lon"], limit=8)
    return templates.TemplateResponse("flotte.html", _ctx(request, fleet=fleet, kiwis=kiwis))


@app.get("/a-propos", response_class=HTMLResponse)
async def about_page(request: Request):
    return templates.TemplateResponse("about.html", _ctx(request))


@app.get("/media/{vacation_id}/{filename}")
async def media(vacation_id: str, filename: str):
    path = store.media_path(vacation_id, filename, CFG)
    if not path:
        raise HTTPException(404)
    return FileResponse(path)


@app.get("/api/vacations")
async def api_vacations():
    return store.list_vacations(CFG)


@app.get("/api/vacations/{vacation_id}")
async def api_vacation(vacation_id: str):
    meta = store.get_vacation(vacation_id, CFG)
    if not meta:
        raise HTTPException(404)
    meta.pop("_dir", None)
    return meta


@app.post("/api/vacations/record")
async def api_record(x_admin_token: str | None = Header(default=None, alias="X-Admin-Token")):
    expected = (CFG.get("web") or {}).get("admin_token") or os.environ.get("GGR_ADMIN_TOKEN") or ""
    if not expected or x_admin_token != expected:
        raise HTTPException(403, "Jeton administrateur invalide")
    if store.recording_in_progress(CFG):
        raise HTTPException(409, "Enregistrement déjà en cours")
    asyncio.create_task(run_vacation(CFG, reason="api"))
    return JSONResponse({"ok": True, "status": "started"}, status_code=202)


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
