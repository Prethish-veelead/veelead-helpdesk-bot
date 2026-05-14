"""
scheduler.py — Background scheduler for periodic SharePoint sync.

Runs inside the same FastAPI process — no extra service needed.
Uses APScheduler's BackgroundScheduler (non-blocking, daemon threads).

Schedules:
  - run_sync() every SYNC_INTERVAL_MINUTES (default 60)
  - cache_clear_expired() once per day at 03:00 (cheap cleanup)

Lifecycle:
  - start_scheduler() called from FastAPI startup
  - stop_scheduler() called from FastAPI shutdown (graceful exit)

If a sync is already running when the next tick fires, APScheduler
will skip it (max_instances=1) — prevents overlapping syncs.
"""

import logging
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger

from config import settings
from storage import cache

log = logging.getLogger(__name__)


# Module-level scheduler instance (singleton)
_scheduler: Optional[BackgroundScheduler] = None


def _sync_job():
    """Run an incremental sync. Errors are logged but don't crash the scheduler."""
    # Imported lazily to avoid circular imports (app.py imports scheduler)
    from app import run_sync
    try:
        log.info("[scheduler] tick — running sync")
        result = run_sync(force_full=False)
        log.info(f"[scheduler] sync result: {result}")
    except Exception as e:
        log.exception(f"[scheduler] sync job crashed: {e}")


def _cache_cleanup_job():
    """Daily cleanup of expired cache entries."""
    try:
        n = cache.cache_clear_expired()
        log.info(f"[scheduler] daily cache cleanup: removed {n} expired entries")
    except Exception as e:
        log.exception(f"[scheduler] cache cleanup failed: {e}")


def start_scheduler() -> BackgroundScheduler:
    """
    Start the background scheduler. Idempotent — safe to call multiple times.
    Returns the scheduler instance for inspection.
    """
    global _scheduler

    if _scheduler is not None and _scheduler.running:
        log.info("[scheduler] already running")
        return _scheduler

    _scheduler = BackgroundScheduler(
        daemon=True,                       # don't block process shutdown
        timezone="UTC",
        job_defaults={
            "coalesce": True,              # if missed multiple ticks, run once
            "max_instances": 1,            # no overlapping runs of same job
            "misfire_grace_time": 300,     # 5 min grace for late starts
        },
    )

    # ── Job 1: periodic sync ──
    interval_min = settings.sync_interval_minutes
    _scheduler.add_job(
        _sync_job,
        trigger=IntervalTrigger(minutes=interval_min),
        id="sync_job",
        name=f"SharePoint sync every {interval_min} min",
        next_run_time=None,                # don't run immediately on startup
                                            # (startup hook already syncs once)
        replace_existing=True,
    )

    # ── Job 2: daily cache cleanup (03:00 UTC) ──
    _scheduler.add_job(
        _cache_cleanup_job,
        trigger=CronTrigger(hour=3, minute=0),
        id="cache_cleanup",
        name="Daily expired-cache cleanup",
        replace_existing=True,
    )

    _scheduler.start()
    log.info(
        f"[scheduler] started — sync every {interval_min} min, "
        f"cache cleanup daily at 03:00 UTC"
    )

    # Log the next scheduled run for visibility
    for job in _scheduler.get_jobs():
        log.info(f"[scheduler]   • {job.name} → next run: {job.next_run_time}")

    return _scheduler


def stop_scheduler() -> None:
    """Gracefully stop the scheduler. Called from FastAPI shutdown."""
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
        log.info("[scheduler] stopped")
    _scheduler = None


def get_scheduler_status() -> dict:
    """For /health endpoint — show job status."""
    if _scheduler is None or not _scheduler.running:
        return {"running": False, "jobs": []}

    return {
        "running": True,
        "timezone": str(_scheduler.timezone),
        "jobs": [
            {
                "id": job.id,
                "name": job.name,
                "next_run_time": (
                    job.next_run_time.isoformat() if job.next_run_time else None
                ),
            }
            for job in _scheduler.get_jobs()
        ],
    }
