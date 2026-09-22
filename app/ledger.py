"""The Truth Ledger — record claims, attach evidence, verify, except.

This is the one path through which every accountable statement in T2R OS flows,
so a CEO report can always tell self-report from verified fact. The invariant is
enforced here in code, not by convention:

    A claim reaches VERIFIED *only* through :func:`verify_claim` with a recorded,
    passing Verification that reaches the level the claim type requires. No other
    function can set it. :func:`record_claim` and :func:`attach_evidence` can
    never produce VERIFIED.

Every transition emits an append-only event, and the original claim and its
evidence are never mutated away — state only moves forward, auditably.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.events import (
    CLAIM_RECORDED,
    CLAIM_STATE_CHANGED,
    EVIDENCE_ATTACHED,
    VERIFICATION_EXCEPTION_RAISED,
    VERIFICATION_EXCEPTION_RESOLVED,
    VERIFICATION_RECORDED,
    record_event,
)
from app.models import Claim, Evidence, Verification, VerificationException
from app.roles import Role
from app.truth import (
    ProvenanceState,
    VerificationLevel,
    required_level,
)

# Verification outcomes.
PASS = "pass"  # noqa: S105 - a verification outcome, not a secret
FAIL = "fail"
INCONCLUSIVE = "inconclusive"


async def _emit_state_change(
    session: AsyncSession, claim: Claim, old: str, actor_person_id: int | None, reason: str
) -> None:
    await record_event(
        session,
        event_type=CLAIM_STATE_CHANGED,
        subject_person_id=claim.subject_person_id,
        actor_person_id=actor_person_id,
        payload={
            "claim_id": claim.id,
            "from": old,
            "to": claim.provenance_state,
            "level": claim.verification_level,
            "reason": reason,
        },
        evidence_ref=f"claim:{claim.id}",
    )


async def record_claim(
    session: AsyncSession,
    *,
    claim_type: str,
    statement: dict[str, object],
    subject_person_id: int | None,
    asserted_by_person_id: int | None = None,
    asserted_role: Role = Role.SYSTEM,
    origin: str = "self",
    occurred_at: datetime | None = None,
    evidence_ref: str | None = None,
) -> Claim:
    """Record a new claim.

    ``origin="system"`` means T2R received the fact directly (e.g. a bot form),
    so it starts SYSTEM_RECORDED at V2 — there is no self-report gap. Everything
    else starts SELF_REPORTED at V0. Neither is VERIFIED.
    """
    moment = occurred_at or datetime.now(UTC)
    if origin == "system":
        state = ProvenanceState.SYSTEM_RECORDED
        level = VerificationLevel.V2_SYSTEM_CORRELATED
    else:
        state = ProvenanceState.SELF_REPORTED
        level = VerificationLevel.V0_CLAIM_ONLY

    claim = Claim(
        claim_type=claim_type,
        subject_person_id=subject_person_id,
        asserted_by_person_id=asserted_by_person_id,
        asserted_role=asserted_role.value,
        statement=statement,
        provenance_state=state.value,
        verification_level=int(level),
        required_level=int(required_level(claim_type)),
        occurred_at=moment,
        evidence_ref=evidence_ref,
    )
    session.add(claim)
    await session.flush()

    await record_event(
        session,
        event_type=CLAIM_RECORDED,
        subject_person_id=subject_person_id,
        actor_person_id=asserted_by_person_id,
        actor_role=asserted_role,
        payload={"claim_type": claim_type, "state": state.value, "level": int(level)},
        evidence_ref=f"claim:{claim.id}",
        occurred_at=moment,
    )
    return claim


async def attach_evidence(
    session: AsyncSession,
    *,
    claim: Claim,
    kind: str,
    ref: str,
    submitted_by_person_id: int | None = None,
    note: str | None = None,
) -> Evidence:
    """Attach evidence. Raises strength to at least V1/EVIDENCE_SUBMITTED — but
    NEVER to VERIFIED. Evidence is a claim's support, not its confirmation."""
    evidence = Evidence(
        claim_id=claim.id,
        kind=kind,
        ref=ref,
        submitted_by_person_id=submitted_by_person_id,
        note=note,
    )
    session.add(evidence)

    old = claim.provenance_state
    # Do not downgrade a system-recorded/verified claim; only lift a bare claim.
    if claim.provenance_state in (
        ProvenanceState.SELF_REPORTED.value,
        ProvenanceState.VERIFICATION_FAILED.value,
        ProvenanceState.UNVERIFIABLE.value,
    ):
        claim.provenance_state = ProvenanceState.EVIDENCE_SUBMITTED.value
    claim.verification_level = max(
        claim.verification_level, int(VerificationLevel.V1_EVIDENCE_ATTACHED)
    )
    await session.flush()

    await record_event(
        session,
        event_type=EVIDENCE_ATTACHED,
        subject_person_id=claim.subject_person_id,
        actor_person_id=submitted_by_person_id,
        payload={"claim_id": claim.id, "kind": kind},
        evidence_ref=f"evidence:{evidence.id}",
    )
    if claim.provenance_state != old:
        await _emit_state_change(session, claim, old, submitted_by_person_id, "evidence_attached")
    return evidence


async def verify_claim(
    session: AsyncSession,
    *,
    claim: Claim,
    method: str,
    outcome: str,
    level_reached: VerificationLevel,
    checked_by_person_id: int | None = None,
    note: str | None = None,
) -> Verification:
    """Record an independent check and move the claim's state accordingly.

    This is the ONLY function that can set VERIFIED, and only when the check
    passed AND reached the level the claim type requires. A failed check yields
    VERIFICATION_FAILED; an inconclusive one yields UNVERIFIABLE. The claim's
    statement is never altered.
    """
    verification = Verification(
        claim_id=claim.id,
        method=method,
        checked_by_person_id=checked_by_person_id,
        outcome=outcome,
        level_reached=int(level_reached),
        note=note,
    )
    session.add(verification)

    old = claim.provenance_state
    needed = required_level(claim.claim_type)
    if outcome == PASS and int(level_reached) >= int(needed):
        claim.provenance_state = ProvenanceState.VERIFIED.value
        claim.verification_level = int(level_reached)
    elif outcome == PASS:
        # Passed, but not strong enough to count as fact yet — keep the evidence
        # level but do not mark VERIFIED.
        claim.verification_level = max(claim.verification_level, int(level_reached))
    elif outcome == FAIL:
        claim.provenance_state = ProvenanceState.VERIFICATION_FAILED.value
    else:  # inconclusive
        claim.provenance_state = ProvenanceState.UNVERIFIABLE.value
    await session.flush()

    await record_event(
        session,
        event_type=VERIFICATION_RECORDED,
        subject_person_id=claim.subject_person_id,
        actor_person_id=checked_by_person_id,
        payload={
            "claim_id": claim.id,
            "method": method,
            "outcome": outcome,
            "level_reached": int(level_reached),
        },
        evidence_ref=f"verification:{verification.id}",
    )
    if claim.provenance_state != old:
        await _emit_state_change(session, claim, old, checked_by_person_id, f"verify:{outcome}")
    return verification


async def raise_verification_exception(
    session: AsyncSession,
    *,
    claim: Claim,
    reason_code: str,
    detail: str = "",
    reviewer_person_id: int | None = None,
) -> VerificationException:
    """Record a conflict as an exception (not an accusation) and mark the claim
    DISPUTED. The claim is preserved untouched."""
    exception = VerificationException(
        claim_id=claim.id,
        reason_code=reason_code,
        detail=detail,
        status="open",
        reviewer_person_id=reviewer_person_id,
    )
    session.add(exception)

    old = claim.provenance_state
    claim.provenance_state = ProvenanceState.DISPUTED.value
    await session.flush()

    await record_event(
        session,
        event_type=VERIFICATION_EXCEPTION_RAISED,
        subject_person_id=claim.subject_person_id,
        actor_person_id=reviewer_person_id,
        payload={"claim_id": claim.id, "reason_code": reason_code},
        evidence_ref=f"vexception:{exception.id}",
    )
    await _emit_state_change(session, claim, old, reviewer_person_id, f"exception:{reason_code}")
    return exception


async def resolve_exception(
    session: AsyncSession,
    *,
    exception: VerificationException,
    reviewer_person_id: int,
    resolution: str,
    new_state: ProvenanceState,
    reason: str = "manual_override",
    now: datetime | None = None,
) -> VerificationException:
    """Resolve an exception with an explicit outcome. A manual override preserves
    the prior claim state, the actor, the time and the reason via an event —
    there are no silent corrections."""
    moment = now or datetime.now(UTC)
    claim = await session.get(Claim, exception.claim_id)
    old = claim.provenance_state if claim else None

    exception.status = "resolved"
    exception.reviewer_person_id = reviewer_person_id
    exception.resolution = resolution
    exception.resolved_at = moment

    if claim is not None:
        claim.provenance_state = new_state.value
    await session.flush()

    await record_event(
        session,
        event_type=VERIFICATION_EXCEPTION_RESOLVED,
        subject_person_id=claim.subject_person_id if claim else None,
        actor_person_id=reviewer_person_id,
        payload={
            "exception_id": exception.id,
            "claim_id": exception.claim_id,
            "from": old,
            "to": new_state.value,
            "reason": reason,
            "resolution": resolution,
        },
        evidence_ref=f"vexception:{exception.id}",
        occurred_at=moment,
    )
    return exception


async def provenance_summary(
    session: AsyncSession, claim_type: str | None = None
) -> dict[str, int]:
    """Count claims by provenance state — so a CEO figure can always show how
    much of it is self-reported versus independently verified."""
    stmt = select(Claim.provenance_state, func.count()).group_by(Claim.provenance_state)
    if claim_type is not None:
        stmt = stmt.where(Claim.claim_type == claim_type)
    rows = (await session.execute(stmt)).all()
    return {state: int(count) for state, count in rows}
