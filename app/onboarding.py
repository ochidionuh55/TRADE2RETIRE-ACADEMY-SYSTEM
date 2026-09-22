"""Staff onboarding — link a Telegram account to a seeded staff identity.

Each staff member is seeded as a canonical Person (with roles) plus a one-time
``join_code``. When they send ``/join CODE`` the code binds their Telegram id to
that staff Person, so they inherit their real roles. The code is single-use and
cleared on link. We never guess an identity — linking is always an explicit act.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Person, StaffProfile
from app.roles import Role


async def link_staff_by_code(
    session: AsyncSession, *, join_code: str, telegram_id: int, full_name: str = ""
) -> tuple[str, Person | None]:
    """Bind ``telegram_id`` to the staff person that holds ``join_code``.

    Returns (status, person). status is one of: "linked", "already_linked",
    "invalid".
    """
    code = (join_code or "").strip().upper()
    if not code:
        return "invalid", None

    profile = (
        await session.execute(
            select(StaffProfile).where(
                StaffProfile.join_code == code, StaffProfile.linked.is_(False)
            )
        )
    ).scalar_one_or_none()
    if profile is None:
        return "invalid", None

    staff_person = await session.get(Person, profile.person_id)
    if staff_person is None:
        return "invalid", None

    # Already this person's account?
    if staff_person.telegram_id == telegram_id:
        profile.linked = True
        profile.join_code = None
        await session.flush()
        return "already_linked", staff_person

    # Release the id from any shell Person created earlier (e.g. by /start),
    # since telegram_id is unique. That empty Person becomes inert.
    holder = (
        await session.execute(select(Person).where(Person.telegram_id == telegram_id))
    ).scalar_one_or_none()
    if holder is not None and holder.id != staff_person.id:
        holder.telegram_id = None
        await session.flush()

    staff_person.telegram_id = telegram_id
    if full_name and not staff_person.full_name:
        staff_person.full_name = full_name
    profile.linked = True
    profile.join_code = None
    await session.flush()
    return "linked", staff_person


async def staff_roster(session: AsyncSession) -> list[dict[str, object]]:
    """Every staff member with link status and (if unlinked) their join code —
    for an admin to distribute."""
    rows = (
        await session.execute(
            select(StaffProfile, Person)
            .join(Person, Person.id == StaffProfile.person_id)
            .where(StaffProfile.is_demo.is_(False))
            .order_by(StaffProfile.title)
        )
    ).all()
    out: list[dict[str, object]] = []
    for profile, person in rows:
        out.append(
            {
                "name": person.full_name,
                "title": profile.title,
                "linked": profile.linked,
                "join_code": profile.join_code,
            }
        )
    return out


async def roles_for(session: AsyncSession, person: Person) -> set[Role]:
    """Active roles held by a person (for a friendly post-link confirmation)."""
    from app.models import RoleAssignment

    values = (
        await session.execute(
            select(RoleAssignment.role).where(
                RoleAssignment.person_id == person.id, RoleAssignment.active.is_(True)
            )
        )
    ).scalars().all()
    out: set[Role] = set()
    for v in values:
        try:
            out.add(Role(v))
        except ValueError:
            continue
    return out
