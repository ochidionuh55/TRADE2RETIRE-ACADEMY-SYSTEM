#!/usr/bin/env python3
"""Seed Trade2Retire's real organisation into the canonical store.

Creates the departments, staff people (no Telegram id yet — they link on
onboarding), their staff profiles and role assignments from app.org. Reporting
lines are created only where a founder has confirmed them.

Idempotent: safe to run repeatedly. Nothing here is a demo record.

    python scripts/seed_org.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import session_scope
from app.migrate import run_migrations
from app.org import seed_org


async def main() -> int:
    await run_migrations()
    async with session_scope() as s:
        summary = await seed_org(s)
    print(
        "Seeded organisation: "
        f"{summary['departments']} departments, "
        f"{summary['staff']} staff, "
        f"{summary['confirmed_reporting_lines']} confirmed reporting lines."
    )
    print("Reporting lines and schedules stay unconfirmed until a founder approves them.")
    print("Staff link to their Telegram accounts at onboarding (a later increment).")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
