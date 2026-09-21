"""Increment 2 · Slice 1 — Foundation invariants.

Proves:
  • RBAC gates management/staff by role, server-side (not by hidden buttons).
  • Organisation seeding is idempotent (re-runs never duplicate).
  • Demo records are excluded from every production aggregate (§17).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select

from app.db import session_scope
from app.models import Cohort, Department, Enrolment, FridayReport, Person, StaffProfile
from app.org import seed_org
from app.roles import Role, has_role, is_management, is_manager, is_staff
from app.services import current_week_ending, weekly_brief


def test_rbac_gating_is_role_based() -> None:
    assert not is_management({Role.STUDENT})
    assert is_management({Role.CEO})
    assert is_management({Role.CO_OWNER})
    assert is_management({Role.ADMIN})
    # A support rep is staff but not management or a manager.
    assert is_staff({Role.SUPPORT})
    assert not is_management({Role.SUPPORT})
    assert not is_manager({Role.SUPPORT})
    # A head of support manages a team; a lone student is not staff.
    assert is_manager({Role.HEAD_OF_SUPPORT})
    assert not is_staff({Role.STUDENT})
    assert has_role({Role.MENTOR, Role.ADMIN}, Role.CEO, Role.ADMIN)
    assert not has_role({Role.STUDENT}, Role.CEO, Role.ADMIN)


async def test_seed_org_is_idempotent(db) -> None:
    async with session_scope() as s:
        first = await seed_org(s)
    async with session_scope() as s:
        await seed_org(s)  # second run must not duplicate anything
    async with session_scope() as s:
        depts = (await s.execute(select(func.count()).select_from(Department))).scalar_one()
        staff = (await s.execute(select(func.count()).select_from(StaffProfile))).scalar_one()
    assert first["departments"] == 5
    assert first["staff"] == 8
    assert depts == 5
    assert staff == 8


async def test_demo_records_excluded_from_brief(db) -> None:
    week = current_week_ending()
    async with session_scope() as s:
        cohort = Cohort(name="Real Cohort", programme="The Architect")
        s.add(cohort)
        await s.flush()

        real = Person(full_name="Real Student", is_demo=False)
        demo = Person(full_name="Demo Student", is_demo=True)
        s.add_all([real, demo])
        await s.flush()

        for person in (real, demo):
            s.add(
                Enrolment(
                    person_id=person.id,
                    cohort_id=cohort.id,
                    programme="The Architect",
                    status="active",
                )
            )
            s.add(
                FridayReport(
                    person_id=person.id,
                    week_ending=week,
                    answers={},
                    submitted_at=datetime.now(UTC),
                )
            )

    async with session_scope() as s:
        brief = await weekly_brief(s)

    # Only the real student and their report count; the demo pair is invisible.
    assert brief["active_students"] == 1
    assert brief["reports_submitted"] == 1
