"""The daily operating loop — priorities, completion, and the Daily Close.

Each staff member sets a few priorities for the day (a plan), marks them done
through the day, and closes the day with a short report. The Truth Ledger keeps
the distinction the whole system is built on:

  • A priority is a PLAN — setting it records nothing in the ledger.
  • Marking one done records a completion CLAIM (self-reported, V0): a claim,
    not a proven fact. Evidence can raise it later; it is never auto-verified.
  • The Daily Close is a self-reported (V0) end-of-day report, labelled as such,
    that feeds management as stated — never dressed up as verified fact.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.attendance import work_date
from app.ledger import record_claim
from app.models import DailyClose, Person, Priority
from app.people import primary_actor_role
from app.truth import CLAIM_DAILY_CLOSE, CLAIM_TASK_COMPLETED

MAX_PRIORITIES = 3


async def add_priority(
    session: AsyncSession, *, person: Person, body: str, now: datetime | None = None
) -> tuple[str, Priority | None]:
    """Add a priority for today. Returns (status, priority): "added", "empty"
    (blank text) or "full" (already at the daily cap)."""
    text = (body or "").strip()
    if not text:
        return "empty", None
    today = work_date(now)
    count = (
        await session.execute(
            select(func.count())
            .select_from(Priority)
            .where(
                Priority.person_id == person.id,
                Priority.work_date == today,
                Priority.status.in_(("open", "done")),
            )
        )
    ).scalar_one()
    if count >= MAX_PRIORITIES:
        return "full", None
    priority = Priority(
        person_id=person.id,
        work_date=today,
        body=text[:280],
        position=count,
        status="open",
    )
    session.add(priority)
    await session.flush()
    return "added", priority


async def list_priorities(
    session: AsyncSession, *, person: Person, now: datetime | None = None
) -> list[Priority]:
    today = work_date(now)
    rows = (
        await session.execute(
            select(Priority)
            .where(
                Priority.person_id == person.id,
                Priority.work_date == today,
                Priority.status != "dropped",
            )
            .order_by(Priority.position, Priority.id)
        )
    ).scalars().all()
    return list(rows)


async def complete_priority(
    session: AsyncSession, *, person: Person, priority_id: int, now: datetime | None = None
) -> Priority | None:
    """Mark a priority done and record the completion as a self-reported claim.
    Returns the priority, or None if it isn't this person's."""
    moment = now or datetime.now(UTC)
    priority = await session.get(Priority, priority_id)
    if priority is None or priority.person_id != person.id:
        return None
    if priority.status == "done":
        return priority
    claim = await record_claim(
        session,
        claim_type=CLAIM_TASK_COMPLETED,
        statement={"priority": priority.body, "work_date": priority.work_date.isoformat()},
        subject_person_id=person.id,
        asserted_by_person_id=person.id,
        asserted_role=await primary_actor_role(session, person),
        origin="self",
        occurred_at=moment,
    )
    priority.status = "done"
    priority.claim_id = claim.id
    priority.done_at = moment
    await session.flush()
    return priority


async def has_closed(
    session: AsyncSession, *, person: Person, now: datetime | None = None
) -> bool:
    today = work_date(now)
    found = (
        await session.execute(
            select(DailyClose.id).where(
                DailyClose.person_id == person.id, DailyClose.work_date == today
            )
        )
    ).scalar_one_or_none()
    return found is not None


async def submit_daily_close(
    session: AsyncSession,
    *,
    person: Person,
    summary: str,
    blockers: str | None = None,
    now: datetime | None = None,
) -> tuple[DailyClose, bool]:
    """Record today's close. Idempotent: one per person per day (created=False
    on a repeat, the first stands)."""
    moment = now or datetime.now(UTC)
    today = work_date(moment)
    existing = (
        await session.execute(
            select(DailyClose).where(
                DailyClose.person_id == person.id, DailyClose.work_date == today
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False
    claim = await record_claim(
        session,
        claim_type=CLAIM_DAILY_CLOSE,
        statement={
            "summary": summary,
            "blockers": blockers or "",
            "work_date": today.isoformat(),
        },
        subject_person_id=person.id,
        asserted_by_person_id=person.id,
        asserted_role=await primary_actor_role(session, person),
        origin="self",
        occurred_at=moment,
    )
    close = DailyClose(
        person_id=person.id,
        work_date=today,
        summary=(summary or "").strip(),
        blockers=(blockers or None),
        claim_id=claim.id,
    )
    session.add(close)
    await session.flush()
    return close, True


async def daily_rollup(session: AsyncSession, now: datetime | None = None) -> dict[str, object]:
    """A management view of today's execution: per-person priorities done/total
    and whether they've closed the day."""
    today = work_date(now)
    priorities = (
        await session.execute(
            select(Priority).where(
                Priority.work_date == today, Priority.status != "dropped"
            )
        )
    ).scalars().all()
    closed_ids = set(
        (
            await session.execute(
                select(DailyClose.person_id).where(DailyClose.work_date == today)
            )
        ).scalars().all()
    )

    by_person: dict[int, dict[str, int]] = {}
    for p in priorities:
        stats = by_person.setdefault(p.person_id, {"total": 0, "done": 0})
        stats["total"] += 1
        if p.status == "done":
            stats["done"] += 1

    people: list[dict[str, object]] = []
    for pid in set(by_person) | closed_ids:
        person = await session.get(Person, pid)
        stats = by_person.get(pid, {"total": 0, "done": 0})
        people.append(
            {
                "name": person.full_name if person else f"person {pid}",
                "total": stats["total"],
                "done": stats["done"],
                "closed": pid in closed_ids,
            }
        )
    people.sort(key=lambda r: str(r["name"]).lower())
    return {
        "work_date": today.isoformat(),
        "people": people,
        "planned": len(by_person),
        "closed": len(closed_ids),
    }
