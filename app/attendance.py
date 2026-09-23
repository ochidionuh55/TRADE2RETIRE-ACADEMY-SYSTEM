"""Staff check-in and office-code verification (Slice 3).

A Telegram check-in is only a *claim* of presence. For OFFICE presence we
corroborate it with an independent, office-bound signal: a short-lived rotating
code that exists only where the office displays it. Enter the right code and the
check-in is verified to V2 (system-correlated) through the Truth Ledger; miss it
and the check-in stands as an unverified claim — never a fake "present".

What this architecture can and cannot honestly do (see the founder brief §4):
  • Office challenge (rotating code): SUPPORTED — implemented here.
  • Office network / device presence: NOT securely verifiable from a Telegram +
    cloud architecture, so it is UNSUPPORTED and never simulated. When office
    verification is unavailable, a check-in is UNVERIFIABLE, not falsely present.

Attendance and productivity are separate domains: an approved REMOTE or LEAVE
day is a stated presence type, never counted as absence.
"""

from __future__ import annotations

import hashlib
import hmac
import math
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import PROCESS, get_settings
from app.ledger import INCONCLUSIVE, PASS, attach_evidence, record_claim, verify_claim
from app.models import CheckIn, Person
from app.people import primary_actor_role
from app.truth import CLAIM_CHECK_IN, VerificationLevel

# ── Presence types ───────────────────────────────────────────────────────────
OFFICE = "office"
REMOTE = "remote"
FIELD = "field"
LEAVE = "leave"
SICK = "sick"
TRAINING = "training"
OFF_DUTY = "off_duty"

PRESENCE_TYPES: tuple[str, ...] = (OFFICE, REMOTE, FIELD, LEAVE, SICK, TRAINING, OFF_DUTY)
# Presence types that place a person at work today (expected/among the workforce).
AT_WORK: frozenset[str] = frozenset({OFFICE, REMOTE, FIELD, TRAINING})
# The only presence type an office challenge applies to.
NEEDS_OFFICE_CHALLENGE: frozenset[str] = frozenset({OFFICE})

_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no confusable 0/O/1/I/L


# ── Office code (rotating, short-lived, replay-resistant) ────────────────────


def _window_index(at: datetime, window_minutes: int) -> int:
    return int(at.timestamp()) // (window_minutes * 60)


def _code_for_window(secret: str, window_index: int) -> str:
    digest = hmac.new(secret.encode(), str(window_index).encode(), hashlib.sha256).digest()
    num = int.from_bytes(digest[:8], "big")
    out = []
    for _ in range(6):
        out.append(_CODE_ALPHABET[num % len(_CODE_ALPHABET)])
        num //= len(_CODE_ALPHABET)
    return "".join(out)


def office_enabled() -> bool:
    return bool(get_settings().office_secret)


def current_office_code(now: datetime | None = None) -> str | None:
    """The code to display in the office right now (None if unsupported)."""
    settings = get_settings()
    if not settings.office_secret:
        return None
    moment = now or datetime.now(UTC)
    wi = _window_index(moment, PROCESS.office_code_window_minutes)
    return _code_for_window(settings.office_secret, wi)


def minutes_left_in_window(now: datetime | None = None) -> int:
    moment = now or datetime.now(UTC)
    window_s = PROCESS.office_code_window_minutes * 60
    used = int(moment.timestamp()) % window_s
    return max(0, (window_s - used) // 60)


def is_valid_office_code(code: str, now: datetime | None = None) -> bool:
    """True if ``code`` matches the current or a recent grace window."""
    settings = get_settings()
    if not settings.office_secret or not code:
        return False
    moment = now or datetime.now(UTC)
    wi = _window_index(moment, PROCESS.office_code_window_minutes)
    candidate = code.strip().upper()
    for back in range(PROCESS.office_code_grace_windows + 1):
        if candidate == _code_for_window(settings.office_secret, wi - back):
            return True
    return False


# ── Office geofence (GPS corroboration) ──────────────────────────────────────
# A stronger office-presence signal than a shared code: the staff member's phone
# has to physically be inside the office radius. It is not unspoofable (a mock-
# location app can lie), but it raises the bar well above a code someone can be
# told over the phone. When the office coordinates aren't configured, geofence
# verification is simply unsupported — never simulated.


def office_geofence_enabled() -> bool:
    s = get_settings()
    return s.office_lat is not None and s.office_lng is not None


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lat/lng points, in metres."""
    r = 6_371_000.0  # Earth radius (m)
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def office_distance_m(lat: float, lng: float) -> float | None:
    """Distance from the configured office to (lat, lng), or None if the office
    location isn't configured."""
    s = get_settings()
    if s.office_lat is None or s.office_lng is None:
        return None
    return _haversine_m(s.office_lat, s.office_lng, lat, lng)


def is_within_office(lat: float, lng: float) -> bool | None:
    """True/False if inside the office radius, or None if geofence unsupported."""
    dist = office_distance_m(lat, lng)
    if dist is None:
        return None
    return dist <= get_settings().office_radius_m


# ── Work date (timezone-aware) ───────────────────────────────────────────────


def work_date(now: datetime | None = None) -> date:
    tz = ZoneInfo(get_settings().timezone)
    moment = now or datetime.now(UTC)
    return moment.astimezone(tz).date()


# ── Check-in ─────────────────────────────────────────────────────────────────


async def check_in(
    session: AsyncSession,
    *,
    person: Person,
    presence_type: str,
    office_code: str | None = None,
    lat: float | None = None,
    lng: float | None = None,
    now: datetime | None = None,
) -> tuple[CheckIn, bool]:
    """Record a check-in for today. Idempotent: one attendance per person per
    day — a second call returns the existing record (created=False).

    Returns ``(check_in, created)``.
    """
    moment = now or datetime.now(UTC)
    today = work_date(moment)

    existing = (
        await session.execute(
            select(CheckIn).where(
                CheckIn.person_id == person.id, CheckIn.work_date == today
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False

    # 1) The check-in is first recorded as a CLAIM of presence.
    claim = await record_claim(
        session,
        claim_type=CLAIM_CHECK_IN,
        statement={"presence_type": presence_type, "work_date": today.isoformat()},
        subject_person_id=person.id,
        asserted_by_person_id=person.id,
        asserted_role=await primary_actor_role(session, person),
        origin="self",
        occurred_at=moment,
    )

    # 2) For OFFICE presence, run the independent office challenge. Two honest
    #    signals, strongest first: a GPS position inside the office geofence, or
    #    the short-lived office code. Either corroborates to V2. When neither is
    #    configured we say so (UNVERIFIABLE) rather than fake a "present".
    if presence_type in NEEDS_OFFICE_CHALLENGE:
        geo_on = office_geofence_enabled()
        code_on = office_enabled()
        if not geo_on and not code_on:
            await verify_claim(
                session,
                claim=claim,
                method="office_presence",
                outcome=INCONCLUSIVE,
                level_reached=VerificationLevel.V0_CLAIM_ONLY,
                note="office verification unsupported (no geofence or secret configured)",
            )
        elif lat is not None and lng is not None and geo_on:
            dist = office_distance_m(lat, lng) or 0.0
            radius = get_settings().office_radius_m
            # The location itself is evidence on the claim, kept for audit.
            await attach_evidence(
                session,
                claim=claim,
                kind="gps",
                ref=f"{lat:.6f},{lng:.6f}",
                submitted_by_person_id=person.id,
                note=f"{dist:.0f}m from office (radius {radius}m)",
            )
            if dist <= radius:
                await verify_claim(
                    session,
                    claim=claim,
                    method="office_geofence",
                    outcome=PASS,
                    level_reached=VerificationLevel.V2_SYSTEM_CORRELATED,
                    checked_by_person_id=None,  # the system verified it
                    note=f"within office geofence ({dist:.0f}m)",
                )
            # else: outside the fence → evidence stands, claim stays unverified.
            # Never a false "present"; the person is nudged to pick Remote.
        elif code_on and is_valid_office_code(office_code or "", moment):
            await verify_claim(
                session,
                claim=claim,
                method="office_challenge",
                outcome=PASS,
                level_reached=VerificationLevel.V2_SYSTEM_CORRELATED,
                checked_by_person_id=None,  # the system verified it
                note="valid office code",
            )
        # else: office verification available but not satisfied (no location, or
        # a wrong/absent code) → the claim stays self-reported, never faked.

    verified = claim.provenance_state == "verified"
    check = CheckIn(
        person_id=person.id,
        work_date=today,
        presence_type=presence_type,
        claim_id=claim.id,
        verified=verified,
    )
    session.add(check)
    await session.flush()
    return check, True


async def attendance_today(
    session: AsyncSession, now: datetime | None = None
) -> dict[str, object]:
    """A management view of today's attendance, keyed to canonical records."""
    today = work_date(now)
    rows = (
        await session.execute(
            select(CheckIn).where(CheckIn.work_date == today)
        )
    ).scalars().all()

    verified_present = sum(1 for r in rows if r.presence_type == OFFICE and r.verified)
    unverified_office = sum(1 for r in rows if r.presence_type == OFFICE and not r.verified)
    remote = sum(1 for r in rows if r.presence_type in (REMOTE, FIELD, TRAINING))
    away = sum(1 for r in rows if r.presence_type in (LEAVE, SICK, OFF_DUTY))
    if office_geofence_enabled():
        method = "geofence"
    elif office_enabled():
        method = "office code"
    else:
        method = "unsupported"
    return {
        "work_date": today.isoformat(),
        "checked_in": len(rows),
        "verified_present": verified_present,
        "unverified_office": unverified_office,
        "remote_or_field": remote,
        "approved_away": away,
        "office_verification": (
            "enabled" if (office_enabled() or office_geofence_enabled()) else "unsupported"
        ),
        "office_method": method,
    }
