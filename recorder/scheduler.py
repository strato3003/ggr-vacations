"""Planification quotidienne : bulletin 18:00 TU et buddy call 12:00 TU."""

from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from recorder.config import load_config
from recorder.session import purge_old, run_buddy_call, run_vacation

log = logging.getLogger(__name__)


def _hhmm_lead(time_utc: str, lead_minutes: int) -> tuple[int, int]:
    hh, mm = (time_utc or "00:00").split(":")
    total = int(hh) * 60 + int(mm) - int(lead_minutes)
    if total < 0:
        total += 24 * 60
    return divmod(total, 60)


def _lead(cfg: dict) -> tuple[int, int]:
    sched = cfg.get("schedule") or {}
    return _hhmm_lead(str(sched.get("time_utc") or "18:00"), int(sched.get("lead_minutes") or 1))


def _buddy_lead(cfg: dict) -> tuple[int, int]:
    buddy = cfg.get("buddy") or {}
    return _hhmm_lead(str(buddy.get("time_utc") or "12:00"), int(buddy.get("lead_minutes") or 1))


def _add_buddy_job(scheduler: AsyncIOScheduler, cfg: dict) -> None:
    hour, minute = _buddy_lead(cfg)
    scheduler.add_job(
        run_buddy_call,
        CronTrigger(hour=hour, minute=minute, timezone="UTC"),
        kwargs={"reason": "buddy"},
        id="ggr-buddy",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    log.info("Planification buddy call : %02d:%02d TU", hour, minute)


def build_scheduler(cfg: dict | None = None) -> AsyncIOScheduler:
    cfg = cfg or load_config()
    hour, minute = _lead(cfg)
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        run_vacation,
        CronTrigger(hour=hour, minute=minute, timezone="UTC"),
        kwargs={"reason": "schedule"},
        id="ggr-vacation",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        purge_old,
        CronTrigger(hour=4, minute=10, timezone="UTC"),
        id="ggr-purge",
        replace_existing=True,
    )
    log.info("Planification : bulletin quotidien %02d:%02d TU", hour, minute)
    if (cfg.get("buddy") or {}).get("enabled", True):
        _add_buddy_job(scheduler, cfg)
    return scheduler


def apply_vacation_schedule(scheduler: AsyncIOScheduler, cfg: dict) -> None:
    """Recale le cron après un changement d’heure / d’avance dans Réglages."""
    hour, minute = _lead(cfg)
    scheduler.reschedule_job(
        "ggr-vacation",
        trigger=CronTrigger(hour=hour, minute=minute, timezone="UTC"),
    )
    log.info("Planification bulletin mise à jour : %02d:%02d TU", hour, minute)
    apply_buddy_schedule(scheduler, cfg)


def apply_buddy_schedule(scheduler: AsyncIOScheduler, cfg: dict) -> None:
    enabled = bool((cfg.get("buddy") or {}).get("enabled", True))
    existing = scheduler.get_job("ggr-buddy")
    if not enabled:
        if existing:
            scheduler.remove_job("ggr-buddy")
            log.info("Buddy call désactivé")
        return
    hour, minute = _buddy_lead(cfg)
    if existing:
        scheduler.reschedule_job(
            "ggr-buddy",
            trigger=CronTrigger(hour=hour, minute=minute, timezone="UTC"),
        )
        log.info("Planification buddy mise à jour : %02d:%02d TU", hour, minute)
        return
    _add_buddy_job(scheduler, cfg)
