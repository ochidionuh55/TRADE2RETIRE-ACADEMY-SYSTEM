"""Slice 4 — the daily operating loop (priorities, completion, Daily Close).

The truth discipline holds here too: a priority is a plan, completing it is a
self-reported claim (never auto-verified), and the Daily Close is self-reported.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.daily import (
    MAX_PRIORITIES,
    add_priority,
    complete_priority,
    daily_rollup,
    has_closed,
    list_priorities,
    submit_daily_close,
)
from app.db import session_scope
from app.models import Claim, Person
from app.truth import CLAIM_DAILY_CLOSE, CLAIM_TASK_COMPLETED, ProvenanceState

NOW = datetime(2026, 9, 22, 9, 0, tzinfo=UTC)


async def _person(s, name="Somto") -> Person:
    p = Person(full_name=name)
    s.add(p)
    await s.flush()
    return p


async def test_priorities_capped_at_max(db) -> None:
    async with session_scope() as s:
        person = await _person(s)
        for i in range(MAX_PRIORITIES):
            status, _ = await add_priority(s, person=person, body=f"task {i}", now=NOW)
            assert status == "added"
        overflow, _ = await add_priority(s, person=person, body="one too many", now=NOW)
        assert overflow == "full"
        prios = await list_priorities(s, person=person, now=NOW)
    assert len(prios) == MAX_PRIORITIES


async def test_empty_priority_rejected(db) -> None:
    async with session_scope() as s:
        person = await _person(s)
        status, p = await add_priority(s, person=person, body="   ", now=NOW)
    assert status == "empty"
    assert p is None


async def test_completing_a_priority_records_a_self_reported_claim(db) -> None:
    async with session_scope() as s:
        person = await _person(s)
        _, p = await add_priority(s, person=person, body="call 10 leads", now=NOW)
        done = await complete_priority(s, person=person, priority_id=p.id, now=NOW)
        assert done.status == "done"
        assert done.claim_id is not None
        claim = await s.get(Claim, done.claim_id)
    assert claim.claim_type == CLAIM_TASK_COMPLETED
    # A completion is a CLAIM, never auto-verified.
    assert claim.provenance_state == ProvenanceState.SELF_REPORTED.value


async def test_cannot_complete_someone_elses_priority(db) -> None:
    async with session_scope() as s:
        a = await _person(s, "Alpha")
        b = await _person(s, "Bravo")
        _, p = await add_priority(s, person=a, body="a's task", now=NOW)
        result = await complete_priority(s, person=b, priority_id=p.id, now=NOW)
    assert result is None


async def test_daily_close_is_idempotent_and_self_reported(db) -> None:
    async with session_scope() as s:
        person = await _person(s)
        close1, created1 = await submit_daily_close(
            s, person=person, summary="shipped the report", blockers=None, now=NOW
        )
        close2, created2 = await submit_daily_close(
            s, person=person, summary="different", now=NOW
        )
        assert created1 is True
        assert created2 is False
        assert close2.id == close1.id  # first stands
        assert await has_closed(s, person=person, now=NOW) is True
        claim = await s.get(Claim, close1.claim_id)
    assert claim.claim_type == CLAIM_DAILY_CLOSE
    assert claim.provenance_state == ProvenanceState.SELF_REPORTED.value


async def test_daily_rollup_counts_done_and_closes(db) -> None:
    async with session_scope() as s:
        person = await _person(s, "Favour")
        _, p1 = await add_priority(s, person=person, body="p1", now=NOW)
        await add_priority(s, person=person, body="p2", now=NOW)
        await complete_priority(s, person=person, priority_id=p1.id, now=NOW)
        await submit_daily_close(s, person=person, summary="done for the day", now=NOW)
    async with session_scope() as s:
        data = await daily_rollup(s, now=NOW)
    assert data["planned"] == 1
    assert data["closed"] == 1
    row = next(r for r in data["people"] if r["name"] == "Favour")
    assert row["total"] == 2
    assert row["done"] == 1
    assert row["closed"] is True
