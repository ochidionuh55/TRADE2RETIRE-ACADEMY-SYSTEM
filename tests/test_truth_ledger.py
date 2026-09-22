"""Slice 2 — Truth Ledger invariants.

North star: CLAIM ≠ VERIFIED FACT. These tests prove the epistemology holds in
code — a self-report can never silently become verified, a failed/uncheckable
claim never becomes a false positive, and a manual override preserves history.
"""

from __future__ import annotations

from sqlalchemy import func, select

from app.db import session_scope
from app.ledger import (
    FAIL,
    INCONCLUSIVE,
    PASS,
    attach_evidence,
    provenance_summary,
    raise_verification_exception,
    record_claim,
    resolve_exception,
    verify_claim,
)
from app.models import Event, Person, Verification
from app.roles import Role
from app.truth import (
    CLAIM_CHECK_IN,
    CLAIM_FRIDAY_REPORT,
    ProvenanceState,
    VerificationLevel,
)


async def _person(s, name="Favour") -> Person:
    p = Person(full_name=name)
    s.add(p)
    await s.flush()
    return p


async def test_self_report_starts_as_claim_only(db) -> None:
    async with session_scope() as s:
        favour = await _person(s)
        claim = await record_claim(
            s,
            claim_type=CLAIM_CHECK_IN,
            statement={"said": "I'm at work"},
            subject_person_id=favour.id,
            asserted_by_person_id=favour.id,
            asserted_role=Role.SUPPORT,
            origin="self",
        )
        assert claim.provenance_state == ProvenanceState.SELF_REPORTED.value
        assert claim.verification_level == int(VerificationLevel.V0_CLAIM_ONLY)


async def test_system_recorded_claim_is_correlated_not_verified(db) -> None:
    async with session_scope() as s:
        student = await _person(s, "Student")
        claim = await record_claim(
            s,
            claim_type=CLAIM_FRIDAY_REPORT,
            statement={"lesson": "patience"},
            subject_person_id=student.id,
            origin="system",  # T2R received the report directly
        )
        # System-correlated (V2) because T2R holds the record — but still not the
        # human-verified state.
        assert claim.provenance_state == ProvenanceState.SYSTEM_RECORDED.value
        assert claim.verification_level == int(VerificationLevel.V2_SYSTEM_CORRELATED)
        assert claim.provenance_state != ProvenanceState.VERIFIED.value


async def test_evidence_never_reaches_verified(db) -> None:
    async with session_scope() as s:
        favour = await _person(s)
        claim = await record_claim(
            s,
            claim_type=CLAIM_CHECK_IN,
            statement={"said": "I'm at work"},
            subject_person_id=favour.id,
            asserted_by_person_id=favour.id,
            origin="self",
        )
        await attach_evidence(s, claim=claim, kind="photo", ref="tg-file-123")
        assert claim.provenance_state == ProvenanceState.EVIDENCE_SUBMITTED.value
        assert claim.verification_level == int(VerificationLevel.V1_EVIDENCE_ATTACHED)
        # The whole point: evidence supports, it does not confirm.
        assert claim.provenance_state != ProvenanceState.VERIFIED.value


async def test_verification_pass_at_required_level_marks_verified(db) -> None:
    async with session_scope() as s:
        favour = await _person(s)
        claim = await record_claim(
            s,
            claim_type=CLAIM_CHECK_IN,  # requires V2 (system-correlated office challenge)
            statement={"said": "I'm at work"},
            subject_person_id=favour.id,
            asserted_by_person_id=favour.id,
            origin="self",
        )
        await verify_claim(
            s,
            claim=claim,
            method="office_challenge",
            outcome=PASS,
            level_reached=VerificationLevel.V2_SYSTEM_CORRELATED,
        )
        assert claim.provenance_state == ProvenanceState.VERIFIED.value


async def test_verification_pass_below_required_is_not_verified(db) -> None:
    async with session_scope() as s:
        favour = await _person(s)
        claim = await record_claim(
            s,
            claim_type=CLAIM_CHECK_IN,  # requires V2
            statement={"said": "I'm at work"},
            subject_person_id=favour.id,
            origin="self",
        )
        await verify_claim(
            s,
            claim=claim,
            method="weak_signal",
            outcome=PASS,
            level_reached=VerificationLevel.V1_EVIDENCE_ATTACHED,  # below required
        )
        assert claim.provenance_state != ProvenanceState.VERIFIED.value


async def test_failed_and_inconclusive_do_not_become_false_positives(db) -> None:
    async with session_scope() as s:
        a = await _person(s, "A")
        b = await _person(s, "B")
        failed = await record_claim(
            s, claim_type=CLAIM_CHECK_IN, statement={}, subject_person_id=a.id, origin="self"
        )
        await verify_claim(
            s, claim=failed, method="office_challenge", outcome=FAIL,
            level_reached=VerificationLevel.V0_CLAIM_ONLY,
        )
        assert failed.provenance_state == ProvenanceState.VERIFICATION_FAILED.value

        # Infrastructure could not check → UNVERIFIABLE, never a false "present".
        uncheckable = await record_claim(
            s, claim_type=CLAIM_CHECK_IN, statement={}, subject_person_id=b.id, origin="self"
        )
        await verify_claim(
            s, claim=uncheckable, method="office_network", outcome=INCONCLUSIVE,
            level_reached=VerificationLevel.V0_CLAIM_ONLY,
        )
        assert uncheckable.provenance_state == ProvenanceState.UNVERIFIABLE.value


async def test_exception_preserves_claim_and_override_records_history(db) -> None:
    async with session_scope() as s:
        favour = await _person(s)
        reviewer = await _person(s, "Daniel")
        claim = await record_claim(
            s,
            claim_type=CLAIM_CHECK_IN,
            statement={"said": "I'm at work", "at": "09:00"},
            subject_person_id=favour.id,
            asserted_by_person_id=favour.id,
            origin="self",
        )
        original_statement = dict(claim.statement)

        exc = await raise_verification_exception(
            s, claim=claim, reason_code="conflicting_office_signal",
            detail="Check-in at 09:00 but no office challenge scan.",
            reviewer_person_id=reviewer.id,
        )
        assert claim.provenance_state == ProvenanceState.DISPUTED.value
        # The claim itself is never rewritten.
        assert claim.statement == original_statement

        await resolve_exception(
            s, exception=exc, reviewer_person_id=reviewer.id,
            resolution="Confirmed remote day, approved.",
            new_state=ProvenanceState.UNVERIFIABLE,
        )
        assert exc.status == "resolved"

    # A manual override leaves an auditable trail: original state, actor, reason.
    async with session_scope() as s:
        rows = (
            await s.execute(
                select(func.count()).select_from(Event).where(
                    Event.event_type == "claim.exception_resolved"
                )
            )
        ).scalar_one()
        assert rows == 1


async def test_provenance_summary_distinguishes_self_from_verified(db) -> None:
    async with session_scope() as s:
        p = await _person(s)
        await record_claim(
            s, claim_type=CLAIM_CHECK_IN, statement={}, subject_person_id=p.id, origin="self"
        )
        sys_claim = await record_claim(
            s, claim_type=CLAIM_FRIDAY_REPORT, statement={}, subject_person_id=p.id,
            origin="system",
        )
        await verify_claim(
            s, claim=sys_claim, method="direct", outcome=PASS,
            level_reached=VerificationLevel.V3_INDEPENDENTLY_VERIFIED,
        )
    async with session_scope() as s:
        summary = await provenance_summary(s)
    assert summary.get(ProvenanceState.SELF_REPORTED.value) == 1
    assert summary.get(ProvenanceState.VERIFIED.value) == 1
    # No independent verifications should be invented.
    async with session_scope() as s:
        vcount = (await s.execute(select(func.count()).select_from(Verification))).scalar_one()
    assert vcount == 1
