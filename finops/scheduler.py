"""Daily collect → analyze job (§4.9). Runs inside the API process when FINOPS_SCHEDULER=true."""

from __future__ import annotations

from collections.abc import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from finops.config import get_settings


def start_scheduler(job: Callable[[], None]) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        job, CronTrigger(hour=get_settings().schedule_cron_hour, minute=0), id="daily-run",
        max_instances=1, coalesce=True, misfire_grace_time=3600,
    )  # fmt: skip
    scheduler.start()
    return scheduler
