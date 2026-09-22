"""Slice 3 — staff check-in & office-code verification invariants (§19)."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from app.attendance import (
    LEAVE,
    OFFICE,
    REMOTE,
    attendance_today,
    check_in,
    current_office_code,
    is_within_office,
)
from app.config import get_settings
from app.db import session_scope
from app.models import Claim, Evidence, Person
from app.truth import ProvenanceState

NOW = datetime(2026, 9, 22, 9, 0, tzinfo=UTC)

# A stand-in office location (Port Harcourt) for geofence tests.
OFFICE_LAT, OFFICE_LNG = 4.815600, 7.049800


def _enable_office(monkeypatch) -> None:
    monkeypatch.setenv("T2R__OFFICE_SECRET", "office-test-secret")
    get_settings.cache_clear()


def _enable_geofence(monkeypatch, radius: int = 75) -> None:
    monkeypatch.setenv("T2R__OFFICE_LAT", str(OFFICE_LAT))
    monkeypatch.setenv("T2R__OFFICE_LNG", str(OFFICE_LNG))
    monkeypatch.setenv("T2R__OFFICE_RADIUS_M", str(radius))
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


# ── Geofence (GPS corroboration) ─────────────────────────────────────────────


async def test_office_checkin_inside_geofence_is_verified(db, monkeypatch) -> None:
    _enable_geofence(monkeypatch)
    async with session_scope() as s:
        favour = await _person(s)
        # Standing on the office coordinates → distance ~0m, inside the radius.
        check, created = await check_in(
            s,
            person=favour,
            presence_type=OFFICE,
            lat=OFFICE_LAT,
            lng=OFFICE_LNG,
            now=NOW,
        )
        claim = await s.get(Claim, check.claim_id)
        evidence = (
            await s.execute(select(Evidence).where(Evidence.claim_id == claim.id))
        ).scalars().all()
    assert created is True
    assert check.verified is True
    assert claim.provenance_state == ProvenanceState.VERIFIED.value
    # The GPS position is kept as evidence on the claim.
    assert any(e.kind == "gps" for e in evidence)


async def test_office_checkin_outside_geofence_is_not_verified(db, monkeypatch) -> None:
    _enable_geofence(monkeypatch)
    # ~0.02° north of the office ≈ 2.2km away — well outside a 75m radius.
    far_lat = OFFICE_LAT + 0.02
    assert is_within_office(far_lat, OFFICE_LNG) is False
    async with session_scope() as s:
        favour = await _person(s)
        check, _ = await check_in(
            s,
            person=favour,
            presence_type=OFFICE,
            lat=far_lat,
            lng=OFFICE_LNG,
            now=NOW,
        )
        claim = await s.get(Claim, check.claim_id)
        evidence = (
            await s.execute(select(Evidence).where(Evidence.claim_id == claim.id))
        ).scalars().all()
    # Not a fake "present": the claim stands, unverified, with the GPS on record.
    assert check.verified is False
    assert claim.provenance_state != ProvenanceState.VERIFIED.value
    assert any(e.kind == "gps" for e in evidence)


async def test_geofence_takes_precedence_over_code_when_location_shared(db, monkeypatch) -> None:
    # Both signals configured. A valid location verifies even with no code typed.
    _enable_office(monkeypatch)
    _enable_geofence(monkeypatch)
    async with session_scope() as s:
        favour = await _person(s)
        check, _ = await check_in(
            s,
            person=favour,
            presence_type=OFFICE,
            lat=OFFICE_LAT,
            lng=OFFICE_LNG,
            now=NOW,
        )
    assert check.verified is True


async def test_geofence_method_is_reported_to_managers(db, monkeypatch) -> None:
    _enable_geofence(monkeypatch)
    async with session_scope() as s:
        favour = await _person(s)
        await check_in(
            s, person=favour, presence_type=OFFICE, lat=OFFICE_LAT, lng=OFFICE_LNG, now=NOW
        )
    async with session_scope() as s:
        data = await attendance_today(s, now=NOW)
    assert data["office_verification"] == "enabled"
    assert data["office_method"] == "geofence"
    assert data["verified_present"] == 1
