"""Regression tests for the post-5948386 workflow hardening.

These tests lock provenance attribution without changing authorization rules.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.attendance import REMOTE, check_in
from app.daily import add_priority, complete_priority
from app.db import session_scope
from app.models import Claim, Person, RoleAssignment
from app.people import grant_role, primary_actor_role
from app.roles import Role

NOW = datetime(2026, 9, 23, 9, 0, tzinfo=UTC)


async def _person(session, name: str = "Staff") -> Person:
    person = Person(full_name=name)
    session.add(person)
    await session.flush()
    return person


async def test_primary_actor_role_falls_back_to_student(db) -> None:
    async with session_scope() as session:
        person = await _person(session)
        role = await primary_actor_role(session, person)
    assert role is Role.STUDENT


async def test_primary_actor_role_is_deterministic_for_multiple_roles(db) -> None:
    async with session_scope() as session:
        person = await _person(session, "Multi Role")
        await grant_role(session, person.id, Role.SUPPORT)
        await grant_role(session, person.id, Role.CEO)
        role = await primary_actor_role(session, person)
    assert role is Role.CEO


async def test_primary_actor_role_ignores_inactive_assignment(db) -> None:
    async with session_scope() as session:
        person = await _person(session, "Inactive CEO")
        session.add(
            RoleAssignment(person_id=person.id, role=Role.CEO.value, active=False)
        )
        await grant_role(session, person.id, Role.SUPPORT)
        await session.flush()
        role = await primary_actor_role(session, person)
    assert role is Role.SUPPORT


async def test_staff_checkin_claim_uses_staff_role_not_student(db) -> None:
    async with session_scope() as session:
        person = await _person(session, "Operations")
        await grant_role(session, person.id, Role.OPERATIONS)
        check, created = await check_in(
            session, person=person, presence_type=REMOTE, now=NOW
        )
        claim = await session.get(Claim, check.claim_id)

    assert created is True
    assert claim.asserted_role == Role.OPERATIONS.value


async def test_task_completion_claim_uses_staff_role_not_student(db) -> None:
    async with session_scope() as session:
        person = await _person(session, "Support")
        await grant_role(session, person.id, Role.SUPPORT)
        status, priority = await add_priority(
            session, person=person, body="Resolve customer queue", now=NOW
        )
        assert status == "added"
        assert priority is not None

        completed = await complete_priority(
            session, person=person, priority_id=priority.id, now=NOW
        )
        assert completed is not None
        claim = await session.get(Claim, completed.claim_id)

    assert claim.asserted_role == Role.SUPPORT.value
