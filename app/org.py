"""Organisation configuration and seeding — Trade2Retire's real structure.

Everything here is *data*, not logic. The engine never asks "is this Prince?";
it asks "does this person hold role X, and what does the ReportingLine table
say?" So a re-org, a new hire, or a title change is a change to this file (or,
later, an admin screen), never a change to business code.

What we KNOW is seeded as fact. What requires a founder decision — reporting
lines, exact schedules, each person's office/remote arrangement — is created
structurally but marked ``confirmed=False`` and carries a TODO(founder). The
system fails safe: an unconfirmed relationship is supported but never inferred
or acted on as if approved.

Staff are seeded with NO Telegram id. A person is linked to their Telegram
account only when they join through onboarding (a later increment), so we never
guess an identity.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Department, Person, ReportingLine, StaffProfile
from app.people import grant_role
from app.roles import Role

_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no confusable 0/O/1/I/L


def _gen_join_code(length: int = 6) -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(length))


@dataclass(frozen=True)
class DepartmentSeed:
    code: str
    name: str


@dataclass(frozen=True)
class StaffSeed:
    name: str
    title: str
    department_code: str
    roles: tuple[Role, ...]
    # Whom this person reports to, by name, when the founder has confirmed it.
    # Left None for now — reporting lines are unconfirmed (TODO founder).
    reports_to: str | None = None


# ── The org, as confirmed so far ─────────────────────────────────────────────

DEPARTMENTS: tuple[DepartmentSeed, ...] = (
    DepartmentSeed("exec", "Executive"),
    DepartmentSeed("legal", "Legal"),
    DepartmentSeed("academy", "Academy"),
    DepartmentSeed("ops", "Operations"),
    DepartmentSeed("support", "Customer Support"),
)

# Names, titles and roles are confirmed from the founder's brief. Reporting
# relationships are deliberately left out here — see reports_to / TODO(founder).
STAFF: tuple[StaffSeed, ...] = (
    StaffSeed("Mr Phillip Ajaebili", "CEO", "exec", (Role.CEO,)),
    StaffSeed("Mrs Chinwe Ajaebili", "Legal Director", "legal", (Role.LEGAL,)),
    StaffSeed("Prince Ochidi", "Co-owner", "exec", (Role.CO_OWNER, Role.ADMIN)),
    StaffSeed("Bishop Fisayo", "Head of Academy", "academy",
              (Role.HEAD_OF_ACADEMY, Role.MENTOR)),
    StaffSeed("Timi", "Operations Officer", "ops", (Role.OPERATIONS,)),
    StaffSeed("Daniel", "Head of Customer Support", "support", (Role.HEAD_OF_SUPPORT,)),
    StaffSeed("Favour", "Customer Representative", "support", (Role.SUPPORT,)),
    StaffSeed("Somto", "Customer Representative", "support", (Role.SUPPORT,)),
)

# TODO(founder): confirm reporting lines. Candidates the structure supports but
# the system will NOT assume until approved:
#   • Favour, Somto  → Daniel (Head of Customer Support)?
#   • Daniel, Timi, Bishop → CEO / Co-owner?
# Fill this map ({subordinate_name: manager_name}) once confirmed, then re-run
# seeding; lines are created with confirmed=True only for names listed here.
CONFIRMED_REPORTING: dict[str, str] = {}


async def _get_or_create_department(session: AsyncSession, seed: DepartmentSeed) -> Department:
    dept = (
        await session.execute(select(Department).where(Department.code == seed.code))
    ).scalar_one_or_none()
    if dept is None:
        dept = Department(code=seed.code, name=seed.name)
        session.add(dept)
        await session.flush()
    return dept


async def _get_or_create_staff_person(session: AsyncSession, seed: StaffSeed) -> Person:
    # Staff are identified by name here (no Telegram id yet). A production
    # person is never a demo record.
    person = (
        await session.execute(
            select(Person).where(
                Person.full_name == seed.name, Person.is_demo.is_(False)
            )
        )
    ).scalars().first()
    if person is None:
        person = Person(full_name=seed.name, is_demo=False)
        session.add(person)
        await session.flush()
    return person


async def seed_org(session: AsyncSession) -> dict[str, int]:
    """Idempotently seed departments, staff, roles and profiles.

    Safe to run repeatedly: existing rows are found and reused, not duplicated.
    Returns a small summary for logging.
    """
    depts: dict[str, Department] = {}
    for dseed in DEPARTMENTS:
        depts[dseed.code] = await _get_or_create_department(session, dseed)

    staff_by_name: dict[str, Person] = {}
    for sseed in STAFF:
        person = await _get_or_create_staff_person(session, sseed)
        staff_by_name[sseed.name] = person

        profile = (
            await session.execute(
                select(StaffProfile).where(StaffProfile.person_id == person.id)
            )
        ).scalar_one_or_none()
        dept = depts.get(sseed.department_code)
        if profile is None:
            session.add(
                StaffProfile(
                    person_id=person.id,
                    department_id=dept.id if dept else None,
                    title=sseed.title,
                    is_demo=False,
                    join_code=_gen_join_code(),
                )
            )
        else:
            profile.title = sseed.title
            profile.department_id = dept.id if dept else profile.department_id
            # Give an unlinked staff member a join code if they don't have one.
            if profile.join_code is None and not profile.linked:
                profile.join_code = _gen_join_code()

        for role in sseed.roles:
            await grant_role(session, person.id, role)

    # Reporting lines: only create the ones a founder has explicitly confirmed.
    lines = 0
    for subordinate, manager in CONFIRMED_REPORTING.items():
        sub = staff_by_name.get(subordinate)
        mgr = staff_by_name.get(manager)
        if not sub or not mgr:
            continue
        existing = (
            await session.execute(
                select(ReportingLine).where(ReportingLine.person_id == sub.id)
            )
        ).scalar_one_or_none()
        if existing is None:
            session.add(
                ReportingLine(person_id=sub.id, manager_id=mgr.id, confirmed=True)
            )
            lines += 1

    await session.flush()

    # Report ACTUAL row counts, not config lengths — so this telemetry is a
    # canonical reconciliation. Across redeploys these stay constant, which is
    # the standing proof that seeding never duplicates.
    dept_count = (
        await session.execute(select(func.count()).select_from(Department))
    ).scalar_one()
    staff_count = (
        await session.execute(select(func.count()).select_from(StaffProfile))
    ).scalar_one()
    confirmed_count = (
        await session.execute(
            select(func.count())
            .select_from(ReportingLine)
            .where(ReportingLine.confirmed.is_(True))
        )
    ).scalar_one()
    return {
        "departments": dept_count,
        "staff": staff_count,
        "confirmed_reporting_lines": confirmed_count,
    }
