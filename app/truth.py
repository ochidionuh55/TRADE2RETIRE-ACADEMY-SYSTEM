"""The Truth & Verification vocabulary — T2R OS's epistemology.

The system-wide invariant: **CLAIM ≠ VERIFIED FACT**. Something a person says
happened is a claim; it becomes a fact only when evidence and/or an independent
source raise it to the required strength. Every management metric must be able
to say whether a number is self-reported or independently verified.

This module holds the vocabulary and policy (levels, states, required strength
per claim type). The tables and transitions live in models/ledger.
"""

from __future__ import annotations

from enum import Enum, IntEnum


class ProvenanceState(str, Enum):
    """Where a claim sits on the road from assertion to fact."""

    SELF_REPORTED = "self_reported"          # a person said so; nothing else yet
    EVIDENCE_SUBMITTED = "evidence_submitted"  # evidence attached, not yet checked
    SYSTEM_RECORDED = "system_recorded"      # T2R received it directly (e.g. a bot form)
    VERIFIED = "verified"                    # independently confirmed to required level
    UNVERIFIABLE = "unverifiable"            # no independent source exists / infra failed
    DISPUTED = "disputed"                    # conflicting evidence — an exception is open
    VERIFICATION_FAILED = "verification_failed"  # a check ran and did not pass


class VerificationLevel(IntEnum):
    """Evidence strength. Ordered so ``>=`` compares strength directly."""

    V0_CLAIM_ONLY = 0
    V1_EVIDENCE_ATTACHED = 1
    V2_SYSTEM_CORRELATED = 2
    V3_INDEPENDENTLY_VERIFIED = 3


# Provenance states that must NEVER be reachable without a recorded, passing
# Verification at sufficient level. Enforced in ledger.verify_claim.
VERIFIED_STATES: frozenset[ProvenanceState] = frozenset({ProvenanceState.VERIFIED})


# ── Claim types ──────────────────────────────────────────────────────────────
# String keys so new claim types are added without a schema change.

CLAIM_CHECK_IN = "attendance.check_in"
CLAIM_TASK_COMPLETED = "task.completed"
CLAIM_DAILY_CLOSE = "daily_close.submitted"
CLAIM_FRIDAY_REPORT = "friday_report.submitted"
CLAIM_CALLS_MADE = "support.calls_made"
CLAIM_ENQUIRIES_RESOLVED = "support.enquiries_resolved"
CLAIM_PAYMENT_RECEIVED = "finance.payment_received"


# TODO(founder): confirm the verification strength each claim type must reach
# before a CEO report may treat it as fact. These are safe, defensible defaults.
#   • A Friday report the bot received directly is system-correlated (V2) — T2R
#     holds the record, no self-report gap.
#   • "I checked in" is only a claim (V0) until an office challenge corroborates.
#   • "I made 14 calls" is self-report until a phone-system record correlates it.
#   • A payment must reach independent (V3) confirmation before it is fact.
REQUIRED_LEVEL: dict[str, VerificationLevel] = {
    CLAIM_CHECK_IN: VerificationLevel.V2_SYSTEM_CORRELATED,
    CLAIM_TASK_COMPLETED: VerificationLevel.V1_EVIDENCE_ATTACHED,
    CLAIM_DAILY_CLOSE: VerificationLevel.V0_CLAIM_ONLY,
    CLAIM_FRIDAY_REPORT: VerificationLevel.V2_SYSTEM_CORRELATED,
    CLAIM_CALLS_MADE: VerificationLevel.V2_SYSTEM_CORRELATED,
    CLAIM_ENQUIRIES_RESOLVED: VerificationLevel.V2_SYSTEM_CORRELATED,
    CLAIM_PAYMENT_RECEIVED: VerificationLevel.V3_INDEPENDENTLY_VERIFIED,
}

# Fallback when a claim type isn't listed: require evidence at minimum.
DEFAULT_REQUIRED_LEVEL: VerificationLevel = VerificationLevel.V1_EVIDENCE_ATTACHED


def required_level(claim_type: str) -> VerificationLevel:
    return REQUIRED_LEVEL.get(claim_type, DEFAULT_REQUIRED_LEVEL)
