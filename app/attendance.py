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
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import PROCESS, get_settings
from app.ledger import INCONCLUSIVE, PASS, record_claim, verify_claim
from app.models import CheckIn, Person
from app.roles import Role
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
        asserted_role=Role.SYSTEM if person is None else Role.STUDENT,
        origin="self",
        occurred_at=moment,
    )

    # 2) For OFFICE presence, run the independent office challenge.
    if presence_type in NEEDS_OFFICE_CHALLENGE:
        if not office_enabled():
            # We cannot check office presence from this architecture — say so
            # honestly rather than fake a "present". UNVERIFIABLE.
            await verify_claim(
                session,
                claim=claim,
                method="office_challenge",
                outcome=INCONCLUSIVE,
                level_reached=VerificationLevel.V0_CLAIM_ONLY,
                note="office verification unsupported (no secret configured)",
            )
        elif is_valid_office_code(office_code or "", moment):
            await verify_claim(
                session,
                claim=claim,
                method="office_challenge",
                outcome=PASS,
                level_reached=VerificationLevel.V2_SYSTEM_CORRELATED,
                checked_by_person_id=None,  # the system verified it
                note="valid office code",
            )
        # else: invalid/absent code → the claim simply stays self-reported
        # (not an accusation; the person is told to re-check the office code).

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
    return {
        "work_date": today.isoformat(),
        "checked_in": len(rows),
        "verified_present": verified_present,
        "unverified_office": unverified_office,
        "remote_or_field": remote,
        "approved_away": away,
        "office_verification": "enabled" if office_enabled() else "unsupported",
    }
