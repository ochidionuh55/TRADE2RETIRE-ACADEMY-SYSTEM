"""Slice 3 — staff check-in & office-code verification invariants (§19)."""

from __future__ import annotations

from datetime import UTC, datetime

from app.attendance import (
    LEAVE,
    OFFICE,
    REMOTE,
    attendance_today,
    check_in,
    current_office_code,
)
from app.config import get_settings
from app.db import session_scope
from app.models import Claim, Person
from app.truth import ProvenanceState

NOW = datetime(2026, 9, 22, 9, 0, tzinfo=UTC)


def _enable_office(monkeypatch) -> None:
    monkeypatch.setenv("T2R__OFFICE_SECRET", "office-test-secret")
    get_settings.cache_clear()


async def _person(s, name="Favour") -> Person:
    p = Person(full_name=name)
    s.add(p)
    await s.flush()
    return p


async def test_office_checkin_with_valid_code_is_verified(db, monkeypatch) -> None:
    _enable_office(monkeypatch)
    code = current_office_code(NOW)
    async with session_scope() as s:
        favour = await _person(s)
        check, created = await check_in(
            s, person=favour, presence_type=OFFICE, office_code=code, now=NOW
        )
        claim = await s.get(Claim, check.claim_id)
    assert created is True
    assert check.verified is True
    assert claim.provenance_state == ProvenanceState.VERIFIED.value


async def test_replayed_or_wrong_code_is_not_verified(db, monkeypatch) -> None:
    _enable_office(monkeypatch)
    # Yesterday's code — a replay from a past window.
    stale = current_office_code(datetime(2026, 9, 21, 9, 0, tzinfo=UTC))
    async with session_scope() as s:
        favour = await _person(s)
        check, _ = await check_in(
            s, person=favour, presence_type=OFFICE, office_code=stale, now=NOW
        )
    assert check.verified is False


async def test_duplicate_check_in_does_not_duplicate_attendance(db, monkeypatch) -> None:
    _enable_office(monkeypatch)
    async with session_scope() as s:
        favour = await _person(s)
        first, c1 = await check_in(s, person=favour, presence_type=REMOTE, now=NOW)
        second, c2 = await check_in(s, person=favour, presence_type=OFFICE, now=NOW)
    assert c1 is True
    assert c2 is False
    assert second.id == first.id  # same record; the first stands


async def test_approved_leave_is_not_counted_absent(db, monkeypatch) -> None:
    _enable_office(monkeypatch)
    async with session_scope() as s:
        on_leave = await _person(s, "On Leave")
        remote = await _person(s, "Remote Worker")
        await check_in(s, person=on_leave, presence_type=LEAVE, now=NOW)
        await check_in(s, person=remote, presence_type=REMOTE, now=NOW)
    async with session_scope() as s:
        data = await attendance_today(s, now=NOW)
    assert data["approved_away"] == 1   # leave is recorded as away, not absent
    assert data["remote_or_field"] == 1  # remote counts as present-in-mode
    assert data["office_verification"] == "enabled"


async def test_office_unsupported_is_unverifiable_never_faked(db) -> None:
    # No T2R__OFFICE_SECRET set → office verification is honestly unsupported.
    async with session_scope() as s:
        favour = await _person(s)
        check, _ = await check_in(s, person=favour, presence_type=OFFICE, now=NOW)
        claim = await s.get(Claim, check.claim_id)
    assert check.verified is False
    assert claim.provenance_state == ProvenanceState.UNVERIFIABLE.value
