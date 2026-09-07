"""Planification quotidienne 18:00 TU (démarrage lead_minutes avant)."""

from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from recorder.config import load_config
from recorder.session import purge_old, run_vacation

log = logging.getLogger(__name__)


def _lead(cfg: dict) -> tuple[int, int]:
    sched = cfg.get("schedule") or {}
    hh, mm = (sched.get("time_utc") or "18:00").split(":")
    lead = int(sched.get("lead_minutes") or 10)
    total = int(hh) * 60 + int(mm) - lead
    if total < 0:
        total += 24 * 60
    return divmod(total, 60)


def build_scheduler(cfg: dict | None = None) -> AsyncIOScheduler:
    cfg = cfg or load_config()
    hour, minute = _lead(cfg)
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        run_vacation,
        CronTrigger(hour=hour, minute=minute, timezone="UTC"),
        kwargs={"cfg": cfg, "reason": "schedule"},
        id="ggr-vacation",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        purge_old,
        CronTrigger(hour=4, minute=10, timezone="UTC"),
        kwargs={"cfg": cfg},
        id="ggr-purge",
        replace_existing=True,
    )
    log.info("Planification : enregistrement quotidien %02d:%02d TU", hour, minute)
    return scheduler
