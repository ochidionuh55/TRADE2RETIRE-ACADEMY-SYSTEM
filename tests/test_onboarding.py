"""Slice 3.5 — staff onboarding (join codes)."""

from __future__ import annotations

from sqlalchemy import func, select

from app.db import session_scope
from app.models import Person, StaffProfile
from app.onboarding import link_staff_by_code, roles_for, staff_roster
from app.org import seed_org
from app.roles import Role


async def _code_for(s, name: str) -> str:
    prof = (
        await s.execute(
            select(StaffProfile)
            .join(Person, Person.id == StaffProfile.person_id)
            .where(Person.full_name == name)
        )
    ).scalar_one()
    return prof.join_code


async def test_seed_generates_unique_join_codes(db) -> None:
    async with session_scope() as s:
        await seed_org(s)
    async with session_scope() as s:
        codes = (
            await s.execute(select(StaffProfile.join_code))
        ).scalars().all()
        distinct = (
            await s.execute(
                select(func.count(func.distinct(StaffProfile.join_code)))
            )
        ).scalar_one()
    assert all(c for c in codes)          # every staff member has a code
    assert distinct == len(codes)          # all unique


async def test_join_links_account_and_grants_roles(db) -> None:
    async with session_scope() as s:
        await seed_org(s)
    async with session_scope() as s:
        code = await _code_for(s, "Mr Phillip Ajaebili")
        status, person = await link_staff_by_code(
            s, join_code=code, telegram_id=123456, full_name="Phillip"
        )
        assert status == "linked"
        assert person.telegram_id == 123456
        roles = await roles_for(s, person)
        assert Role.CEO in roles
    # The code is single-use: the profile is linked and the code cleared.
    async with session_scope() as s:
        again, _ = await link_staff_by_code(s, join_code=code, telegram_id=999)
        assert again == "invalid"


async def test_join_transfers_id_from_a_prior_shell_person(db) -> None:
    async with session_scope() as s:
        await seed_org(s)
        # A shell Person created earlier by /start holds the Telegram id.
        shell = Person(full_name="Early Start", telegram_id=777)
        s.add(shell)
        await s.flush()
        code = await _code_for(s, "Timi")
        status, staff = await link_staff_by_code(
            s, join_code=code, telegram_id=777, full_name="Timi"
        )
        assert status == "linked"
        assert staff.telegram_id == 777
        assert staff.full_name == "Timi"
    # Exactly one Person now holds 777 — the staff person, not the shell.
    async with session_scope() as s:
        holders = (
            await s.execute(
                select(func.count()).select_from(Person).where(Person.telegram_id == 777)
            )
        ).scalar_one()
    assert holders == 1


async def test_invalid_code_is_rejected(db) -> None:
    async with session_scope() as s:
        await seed_org(s)
        status, person = await link_staff_by_code(s, join_code="NOPE99", telegram_id=1)
    assert status == "invalid"
    assert person is None


async def test_roster_lists_codes_for_unlinked_staff(db) -> None:
    async with session_scope() as s:
        await seed_org(s)
        code = await _code_for(s, "Daniel")
        await link_staff_by_code(s, join_code=code, telegram_id=42, full_name="Daniel")
    async with session_scope() as s:
        roster = await staff_roster(s)
    daniel = next(r for r in roster if r["name"] == "Daniel")
    assert daniel["linked"] is True
    assert daniel["join_code"] is None
    # Someone not yet linked still shows a code.
    unlinked = [r for r in roster if not r["linked"]]
    assert unlinked and all(r["join_code"] for r in unlinked)
