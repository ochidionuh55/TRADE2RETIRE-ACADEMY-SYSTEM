"""Scheduled jobs — the parts of the loop that run on time, not on a tap.

Daily: mark overdue interventions, then apply the missed-report rule. Both are
idempotent, so a double run never double-flags. The CEO pulls the brief on
demand via the bot; a scheduled push can be added once chat targets are known.
"""

from __future__ import annotations

import asyncio

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.db import session_scope
from app.logging import configure_logging, get_logger
from app.migrate import run_migrations
from app.org import seed_org
from app.services import detect_missed_and_intervene, sweep_overdue

logger = get_logger(__name__)


async def ensure_org() -> None:
    """Seed the organisation on boot. Idempotent, and never fatal: a seeding
    failure is logged and the worker still runs its loop."""
    try:
        async with session_scope() as s:
            summary = await seed_org(s)
        logger.info("org.seeded", **summary)
    except Exception as exc:
        # Fail-safe but LOUD: the worker keeps running, but a seeding failure is
        # emitted as error-level telemetry so it surfaces, never disappears.
        logger.error("org.seed_failed", error=str(exc), exc_info=True)


async def daily_pass() -> None:
    async with session_scope() as s:
        overdue = await sweep_overdue(s)
    async with session_scope() as s:
        raised = await detect_missed_and_intervene(s)
    logger.info("worker.daily_pass", overdue=overdue, interventions_raised=len(raised))


async def main() -> None:
    configure_logging()
    await run_migrations()  # Alembic owns the schema (adopt-or-upgrade, locked).
    await ensure_org()
    # Run once on boot so a fresh deploy is immediately current.
    await daily_pass()
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(daily_pass, "cron", hour=6, minute=0, id="daily_pass")
    scheduler.start()
    logger.info("worker.started")
    await asyncio.Event().wait()  # run forever


if __name__ == "__main__":
    asyncio.run(main())
