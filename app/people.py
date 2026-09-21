"""Resolving the person and roles behind a Telegram user.

Access is config- and record-driven: a Telegram id in ``T2R__ADMIN_IDS`` is an
admin; every other capability comes from a stored ``RoleAssignment``. A first
contact creates the canonical ``Person`` so the gate has something to attribute
to, but a bare Person confers no access.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import Person, RoleAssignment
from app.roles import Role


async def resolve_person(
    session: AsyncSession,
    telegram_id: int,
    full_name: str = "",
) -> tuple[Person, set[Role]]:
    """Return the canonical person for a Telegram id and their active roles."""
    person = (
        await session.execute(select(Person).where(Person.telegram_id == telegram_id))
    ).scalar_one_or_none()
    if person is None:
        person = Person(telegram_id=telegram_id, full_name=full_name)
        session.add(person)
        await session.flush()
    elif full_name and not person.full_name:
        person.full_name = full_name

    roles: set[Role] = set()
    rows = (
        await session.execute(
            select(RoleAssignment.role).where(
                RoleAssignment.person_id == person.id, RoleAssignment.active.is_(True)
            )
        )
    ).scalars().all()
    for value in rows:
        try:
            roles.add(Role(value))
        except ValueError:
            continue

    # Config-driven admin. Kept in sync on every contact.
    if telegram_id in get_settings().admin_id_set:
        roles.add(Role.ADMIN)
        roles.add(Role.CEO)

    return person, roles


async def grant_role(session: AsyncSession, person_id: int, role: Role) -> None:
    """Idempotently grant a role (used by seeding and admin tooling)."""
    existing = (
        await session.execute(
            select(RoleAssignment).where(
                RoleAssignment.person_id == person_id, RoleAssignment.role == role.value
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        session.add(RoleAssignment(person_id=person_id, role=role.value, active=True))
    else:
        existing.active = True
