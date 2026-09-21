"""Scheduled jobs — the parts of the loop that run on time, not on a tap.

Daily: mark overdue interventions, then apply the missed-report rule. Both are
idempotent, so a double run never double-flags. The CEO pulls the brief on
demand via the bot; a scheduled push can be added once chat targets are known.
"""

from __future__ import annotations

import asyncio

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.bootstrap import create_schema
from app.db import session_scope
from app.logging import configure_logging, get_logger
from app.services import detect_missed_and_intervene, sweep_overdue

logger = get_logger(__name__)


async def daily_pass() -> None:
    async with session_scope() as s:
        overdue = await sweep_overdue(s)
    async with session_scope() as s:
        raised = await detect_missed_and_intervene(s)
    logger.info("worker.daily_pass", overdue=overdue, interventions_raised=len(raised))


async def main() -> None:
    configure_logging()
    await create_schema()
    # Run once on boot so a fresh deploy is immediately current.
    await daily_pass()
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(daily_pass, "cron", hour=6, minute=0, id="daily_pass")
    scheduler.start()
    logger.info("worker.started")
    await asyncio.Event().wait()  # run forever


if __name__ == "__main__":
    asyncio.run(main())
