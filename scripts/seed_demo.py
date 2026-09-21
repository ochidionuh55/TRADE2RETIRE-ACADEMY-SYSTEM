#!/usr/bin/env python3
"""Seed a small demo cohort so you can see the loop immediately.

Creates one cohort and three students enrolled three weeks ago (so the
missed-report rule has something to fire on), and — if T2R__ADMIN_IDS is set —
makes the first admin id a Person with MENTOR + CEO + ADMIN roles and assigns
them as the demo mentor. Idempotent-ish: safe to run once on a fresh database.

    python scripts/seed_demo.py
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app.bootstrap import create_schema
from app.config import get_settings
from app.db import session_scope
from app.models import Cohort, Enrolment, Person
from app.people import grant_role
from app.roles import Role


async def main() -> int:
    await create_schema()
    settings = get_settings()
    async with session_scope() as s:
        cohort = Cohort(name="Cohort A — The Architect", programme="The Architect")
        s.add(cohort)
        await s.flush()

        mentor_id: int | None = None
        admins = sorted(settings.admin_id_set)
        if admins:
            tg = admins[0]
            mentor = (
                await s.execute(select(Person).where(Person.telegram_id == tg))
            ).scalar_one_or_none()
            if mentor is None:
                mentor = Person(telegram_id=tg, full_name="Academy Mentor")
                s.add(mentor)
                await s.flush()
            for role in (Role.MENTOR, Role.CEO, Role.ADMIN):
                await grant_role(s, mentor.id, role)
            mentor_id = mentor.id

        three_weeks_ago = datetime.now(UTC) - timedelta(weeks=3)
        for name in ("Demo Student One", "Demo Student Two", "Demo Student Three"):
            person = Person(full_name=name)
            s.add(person)
            await s.flush()
            enr = Enrolment(
                person_id=person.id,
                cohort_id=cohort.id,
                mentor_id=mentor_id,
                programme="The Architect",
                status="active",
            )
            enr.created_at = three_weeks_ago
            s.add(enr)

    print("Seeded: 1 cohort, 3 students (enrolled 3 weeks ago).")
    print("Run the worker (or wait for its boot pass) to raise missed-report interventions,")
    print("then open the bot: /queue as the admin/mentor, /brief as CEO.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
