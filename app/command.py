"""T2R Command: management assignments and accountable office correspondence."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ledger import record_claim
from app.models import (
    Assignment,
    AssignmentHistory,
    Correspondence,
    Person,
    RoleAssignment,
    StaffProfile,
)
from app.people import primary_actor_role
from app.roles import MANAGEMENT_ROLES

OPEN_ASSIGNMENT = ("assigned", "accepted", "in_progress", "blocked", "submitted")
OPEN_CORRESPONDENCE = ("open", "acknowledged", "action_required")


def due_for(kind: str, now: datetime | None = None) -> datetime | None:
    moment = now or datetime.now(UTC)
    if kind == "today":
        return moment.replace(hour=17, minute=0, second=0, microsecond=0)
    if kind == "week":
        days = max(0, 4 - moment.weekday())
        return (moment + timedelta(days=days)).replace(hour=17, minute=0, second=0, microsecond=0)
    return None


async def staff_choices(session: AsyncSession) -> list[Person]:
    ids = (
        (await session.execute(select(StaffProfile.person_id).where(StaffProfile.active.is_(True))))
        .scalars()
        .all()
    )
    if not ids:
        return []
    rows = (
        (
            await session.execute(
                select(Person)
                .where(Person.id.in_(ids), Person.active.is_(True), Person.is_demo.is_(False))
                .order_by(Person.full_name)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def management_choices(session: AsyncSession) -> list[Person]:
    wanted = tuple(r.value for r in MANAGEMENT_ROLES)
    ids = (
        (
            await session.execute(
                select(RoleAssignment.person_id).where(
                    RoleAssignment.active.is_(True), RoleAssignment.role.in_(wanted)
                )
            )
        )
        .scalars()
        .all()
    )
    if not ids:
        return []
    rows = (
        (
            await session.execute(
                select(Person)
                .where(Person.id.in_(set(ids)), Person.active.is_(True), Person.is_demo.is_(False))
                .order_by(Person.full_name)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def create_assignment(
    session: AsyncSession,
    *,
    creator: Person,
    assignee_id: int,
    body: str,
    due_kind: str = "today",
    now: datetime | None = None,
) -> Assignment:
    text = (body or "").strip()
    if not text:
        raise ValueError("assignment body is required")
    target = await session.get(Person, assignee_id)
    if target is None or not target.active or target.is_demo:
        raise ValueError("invalid assignee")
    staff = await session.execute(
        select(StaffProfile.id).where(
            StaffProfile.person_id == assignee_id, StaffProfile.active.is_(True)
        )
    )
    if staff.scalar_one_or_none() is None:
        raise ValueError("assignee is not active staff")
    moment = now or datetime.now(UTC)
    item = Assignment(
        created_by_person_id=creator.id,
        assignee_person_id=assignee_id,
        body=text[:2000],
        cadence=due_kind,
        due_at=due_for(due_kind, moment),
        status="assigned",
    )
    session.add(item)
    await session.flush()
    session.add(
        AssignmentHistory(
            assignment_id=item.id,
            actor_person_id=creator.id,
            action="assigned",
            detail=text[:2000],
            occurred_at=moment,
        )
    )
    await session.flush()
    return item


async def list_assignments(
    session: AsyncSession, *, person: Person, include_closed: bool = False
) -> list[Assignment]:
    stmt = select(Assignment).where(Assignment.assignee_person_id == person.id)
    if not include_closed:
        stmt = stmt.where(Assignment.status.in_(OPEN_ASSIGNMENT))
    rows = (
        (await session.execute(stmt.order_by(Assignment.due_at.asc(), Assignment.id.desc())))
        .scalars()
        .all()
    )
    return list(rows)


async def transition_assignment(
    session: AsyncSession,
    *,
    person: Person,
    assignment_id: int,
    action: str,
    note: str = "",
    now: datetime | None = None,
) -> Assignment | None:
    item = await session.get(Assignment, assignment_id)
    if item is None or item.assignee_person_id != person.id:
        return None
    moment = now or datetime.now(UTC)
    transitions = {
        "accept": "accepted",
        "start": "in_progress",
        "block": "blocked",
        "done": "submitted",
    }
    if action not in transitions or item.status not in OPEN_ASSIGNMENT:
        return None
    item.status = transitions[action]
    if action == "done":
        role = await primary_actor_role(session, person)
        claim = await record_claim(
            session,
            claim_type="assignment_completed",
            statement={"assignment_id": item.id, "body": item.body, "note": note},
            subject_person_id=person.id,
            asserted_by_person_id=person.id,
            asserted_role=role,
            origin="self",
            occurred_at=moment,
        )
        item.completion_claim_id = claim.id
        item.completed_at = moment
    session.add(
        AssignmentHistory(
            assignment_id=item.id,
            actor_person_id=person.id,
            action=action,
            detail=(note or "")[:2000],
            occurred_at=moment,
        )
    )
    await session.flush()
    return item


async def create_correspondence(
    session: AsyncSession,
    *,
    sender: Person,
    recipient_id: int,
    category: str,
    body: str,
    parent_id: int | None = None,
    now: datetime | None = None,
) -> Correspondence:
    text = (body or "").strip()
    if not text:
        raise ValueError("message body is required")
    recipient = await session.get(Person, recipient_id)
    if recipient is None or not recipient.active or recipient.is_demo:
        raise ValueError("invalid recipient")
    item = Correspondence(
        sender_person_id=sender.id,
        recipient_person_id=recipient_id,
        category=(category or "report")[:32],
        body=text[:4000],
        status="open",
        parent_id=parent_id,
        occurred_at=now or datetime.now(UTC),
    )
    session.add(item)
    await session.flush()
    return item


async def inbox(session: AsyncSession, *, person: Person, limit: int = 20) -> list[Correspondence]:
    rows = (
        (
            await session.execute(
                select(Correspondence)
                .where(
                    Correspondence.recipient_person_id == person.id,
                    Correspondence.status.in_(OPEN_CORRESPONDENCE),
                )
                .order_by(Correspondence.occurred_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def acknowledge_correspondence(
    session: AsyncSession, *, person: Person, item_id: int
) -> Correspondence | None:
    item = await session.get(Correspondence, item_id)
    if item is None or item.recipient_person_id != person.id:
        return None
    if item.status == "open":
        item.status = "acknowledged"
    await session.flush()
    return item


async def resolve_correspondence(
    session: AsyncSession, *, person: Person, item_id: int, now: datetime | None = None
) -> Correspondence | None:
    item = await session.get(Correspondence, item_id)
    if item is None or item.recipient_person_id != person.id:
        return None
    item.status = "resolved"
    item.resolved_at = now or datetime.now(UTC)
    await session.flush()
    return item
