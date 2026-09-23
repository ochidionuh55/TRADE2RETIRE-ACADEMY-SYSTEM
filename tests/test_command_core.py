"""T2R Command domain invariants."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.command import (
    acknowledge_correspondence,
    create_assignment,
    create_correspondence,
    inbox,
    list_assignments,
    resolve_correspondence,
    transition_assignment,
)
from app.db import session_scope
from app.models import AssignmentHistory, Claim, Person, StaffProfile
from app.people import grant_role
from app.roles import Role

NOW = datetime(2026, 9, 23, 9, 0, tzinfo=UTC)


async def _staff(session, name: str, role: Role) -> Person:
    p = Person(full_name=name)
    session.add(p)
    await session.flush()
    session.add(StaffProfile(person_id=p.id, title=name, active=True))
    await grant_role(session, p.id, role)
    await session.flush()
    return p


async def test_management_assignment_is_attributable_and_visible(db) -> None:
    async with session_scope() as s:
        ceo = await _staff(s, "CEO", Role.CEO)
        worker = await _staff(s, "Worker", Role.SUPPORT)
        item = await create_assignment(
            s, creator=ceo, assignee_id=worker.id, body="Call leads", now=NOW
        )
        rows = await list_assignments(s, person=worker)
        history = (
            (
                await s.execute(
                    select(AssignmentHistory).where(AssignmentHistory.assignment_id == item.id)
                )
            )
            .scalars()
            .all()
        )
    assert rows[0].body == "Call leads"
    assert rows[0].created_by_person_id == ceo.id
    assert history[0].action == "assigned"


async def test_assignment_cannot_target_non_staff(db) -> None:
    async with session_scope() as s:
        ceo = await _staff(s, "CEO", Role.CEO)
        outsider = Person(full_name="Outsider")
        s.add(outsider)
        await s.flush()
        with pytest.raises(ValueError):
            await create_assignment(s, creator=ceo, assignee_id=outsider.id, body="Nope", now=NOW)


async def test_worker_cannot_complete_someone_elses_assignment(db) -> None:
    async with session_scope() as s:
        ceo = await _staff(s, "CEO", Role.CEO)
        a = await _staff(s, "A", Role.SUPPORT)
        b = await _staff(s, "B", Role.SUPPORT)
        item = await create_assignment(s, creator=ceo, assignee_id=a.id, body="Owned by A", now=NOW)
        result = await transition_assignment(
            s, person=b, assignment_id=item.id, action="done", now=NOW
        )
    assert result is None


async def test_completion_is_self_reported_claim_not_verified_fact(db) -> None:
    async with session_scope() as s:
        ceo = await _staff(s, "CEO", Role.CEO)
        worker = await _staff(s, "Worker", Role.SUPPORT)
        item = await create_assignment(
            s, creator=ceo, assignee_id=worker.id, body="Call leads", now=NOW
        )
        done = await transition_assignment(
            s, person=worker, assignment_id=item.id, action="done", now=NOW
        )
        claim = await s.get(Claim, done.completion_claim_id)
    assert done.status == "submitted"
    assert claim.claim_type == "assignment_completed"
    assert claim.asserted_role == Role.SUPPORT.value
    assert claim.verification_level == 0


async def test_correspondence_is_private_to_recipient(db) -> None:
    async with session_scope() as s:
        sender = await _staff(s, "Staff", Role.SUPPORT)
        ceo = await _staff(s, "CEO", Role.CEO)
        other = await _staff(s, "Other", Role.CO_OWNER)
        item = await create_correspondence(
            s, sender=sender, recipient_id=ceo.id, category="blocker", body="Need approval", now=NOW
        )
        assert [x.id for x in await inbox(s, person=ceo)] == [item.id]
        assert await inbox(s, person=other) == []


async def test_only_recipient_can_acknowledge_or_resolve(db) -> None:
    async with session_scope() as s:
        sender = await _staff(s, "Staff", Role.SUPPORT)
        ceo = await _staff(s, "CEO", Role.CEO)
        other = await _staff(s, "Other", Role.CO_OWNER)
        item = await create_correspondence(
            s, sender=sender, recipient_id=ceo.id, category="report", body="Update", now=NOW
        )
        assert await acknowledge_correspondence(s, person=other, item_id=item.id) is None
        assert await resolve_correspondence(s, person=other, item_id=item.id, now=NOW) is None
        ack = await acknowledge_correspondence(s, person=ceo, item_id=item.id)
        assert ack.status == "acknowledged"
        resolved = await resolve_correspondence(s, person=ceo, item_id=item.id, now=NOW)
        assert resolved.status == "resolved"
