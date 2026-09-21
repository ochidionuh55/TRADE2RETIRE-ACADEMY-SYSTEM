"""Recording events — the one write path into the source of truth.

Every meaningful thing that happens becomes an ``Event``: attributable (who or
``system``), timestamped, and pointing at its evidence. Entities are the current
view of this stream; the CEO brief is assembled from it.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Event
from app.roles import Role


async def record_event(
    session: AsyncSession,
    *,
    event_type: str,
    subject_person_id: int | None = None,
    actor_person_id: int | None = None,
    actor_role: Role = Role.SYSTEM,
    payload: dict[str, object] | None = None,
    evidence_ref: str | None = None,
    occurred_at: datetime | None = None,
) -> Event:
    """Append one immutable event. Returns the stored row (not yet committed)."""
    event = Event(
        event_type=event_type,
        actor_person_id=actor_person_id,
        actor_role=actor_role.value,
        subject_person_id=subject_person_id,
        payload=payload or {},
        evidence_ref=evidence_ref,
        occurred_at=occurred_at or datetime.now(UTC),
    )
    session.add(event)
    await session.flush()
    return event


# Canonical event type names — one place, so surfaces never invent their own.
LEAD_CAPTURED = "lead.captured"
STUDENT_ENROLLED = "student.enrolled"
CLASS_ATTENDED = "class.attended"
ASSIGNMENT_SUBMITTED = "assignment.submitted"
ASSESSMENT_PASSED = "assessment.passed"
FRIDAY_REPORT_SUBMITTED = "friday_report.submitted"
MENTOR_REVIEW_COMPLETED = "mentor_review.completed"
FLAG_RAISED = "flag.raised"
INTERVENTION_CREATED = "mentor_intervention.created"
INTERVENTION_COMPLETED = "mentor_intervention.completed"
INTERVENTION_OVERDUE = "intervention.overdue"
MILESTONE_AWARDED = "milestone.awarded"
STUDENT_GRADUATED = "student.graduated"
