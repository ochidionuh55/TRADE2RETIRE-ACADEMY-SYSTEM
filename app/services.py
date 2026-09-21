"""The MVP loop: Friday report → mentor review → intervention → CEO brief.

Every rule here is deterministic and explains itself. No scoring, no
prediction — just the academy's own policy applied to canonical evidence, with
a flag that states the rule it fired on.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import PROCESS
from app.events import (
    FLAG_RAISED,
    FRIDAY_REPORT_SUBMITTED,
    INTERVENTION_COMPLETED,
    INTERVENTION_CREATED,
    INTERVENTION_OVERDUE,
    MENTOR_REVIEW_COMPLETED,
    record_event,
)
from app.models import (
    OPEN_STATES,
    Cohort,
    Enrolment,
    Flag,
    FridayReport,
    Intervention,
    MentorReview,
    Person,
)
from app.roles import Role

MISSED_RULE = "missed_friday_reports"


# ── week maths ───────────────────────────────────────────────────────────────


def current_week_ending(now: datetime | None = None) -> date:
    """The Friday of the current calendar week."""
    today = (now or datetime.now(UTC)).date()
    monday = today - timedelta(days=today.weekday())
    return monday + timedelta(days=PROCESS.report_week_ends_weekday)


def recent_completed_weeks(count: int, now: datetime | None = None) -> list[date]:
    """The ``count`` most recent week-endings strictly before this week's."""
    current = current_week_ending(now)
    return [current - timedelta(weeks=i) for i in range(1, count + 1)]


# ── Friday report intake ─────────────────────────────────────────────────────


async def submit_friday_report(
    session: AsyncSession,
    person: Person,
    answers: dict[str, object],
    now: datetime | None = None,
) -> tuple[FridayReport, bool]:
    """Store one week's report. Idempotent: first submission per week wins."""
    moment = now or datetime.now(UTC)
    week = current_week_ending(moment)

    existing = (
        await session.execute(
            select(FridayReport).where(
                FridayReport.person_id == person.id,
                FridayReport.week_ending == week,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False

    enrolment = (
        await session.execute(
            select(Enrolment).where(
                Enrolment.person_id == person.id, Enrolment.status == "active"
            )
        )
    ).scalars().first()

    report = FridayReport(
        person_id=person.id,
        cohort_id=enrolment.cohort_id if enrolment else None,
        week_ending=week,
        answers=answers,
        submitted_at=moment,
    )
    session.add(report)
    await session.flush()

    await record_event(
        session,
        event_type=FRIDAY_REPORT_SUBMITTED,
        subject_person_id=person.id,
        actor_person_id=person.id,
        actor_role=Role.STUDENT,
        payload={"week_ending": week.isoformat()},
        evidence_ref=f"friday_report:{report.id}",
        occurred_at=moment,
    )
    return report, True


# ── mentor review ────────────────────────────────────────────────────────────


async def review_report(
    session: AsyncSession,
    mentor: Person,
    report_id: int,
    note: str,
    now: datetime | None = None,
) -> MentorReview:
    """Record a mentor's review against a specific report."""
    moment = now or datetime.now(UTC)
    review = MentorReview(report_id=report_id, mentor_id=mentor.id, note=note)
    session.add(review)
    await session.flush()
    report = await session.get(FridayReport, report_id)
    await record_event(
        session,
        event_type=MENTOR_REVIEW_COMPLETED,
        subject_person_id=report.person_id if report else None,
        actor_person_id=mentor.id,
        actor_role=Role.MENTOR,
        evidence_ref=f"mentor_review:{review.id}",
        occurred_at=moment,
    )
    return review


# ── the intervention rule ────────────────────────────────────────────────────


async def detect_missed_and_intervene(
    session: AsyncSession, now: datetime | None = None
) -> list[Intervention]:
    """Raise an intervention for any student who missed N consecutive reports.

    Deterministic and explainable. Idempotent: a student already carrying an
    open intervention for this rule is not flagged again.
    """
    moment = now or datetime.now(UTC)
    weeks = recent_completed_weeks(PROCESS.missed_reports_threshold, moment)
    if not weeks:
        return []
    earliest = min(weeks)

    enrolments = (
        await session.execute(select(Enrolment).where(Enrolment.status == "active"))
    ).scalars().all()

    raised: list[Intervention] = []
    for enr in enrolments:
        # Don't judge a student who wasn't enrolled for the whole window.
        if enr.created_at.date() > earliest:
            continue

        reports = (
            await session.execute(
                select(func.count())
                .select_from(FridayReport)
                .where(
                    FridayReport.person_id == enr.person_id,
                    FridayReport.week_ending.in_(weeks),
                )
            )
        ).scalar_one()
        if reports > 0:
            continue  # submitted at least one — not "consecutive missed"

        already_open = (
            await session.execute(
                select(func.count())
                .select_from(Intervention)
                .where(
                    Intervention.subject_person_id == enr.person_id,
                    Intervention.rule == MISSED_RULE,
                    Intervention.status.in_(tuple(OPEN_STATES)),
                )
            )
        ).scalar_one()
        if already_open:
            continue

        reason = (
            f"No Friday report for {PROCESS.missed_reports_threshold} consecutive "
            f"weeks (through {max(weeks).isoformat()})."
        )
        flag = Flag(subject_person_id=enr.person_id, rule=MISSED_RULE, reason=reason)
        session.add(flag)
        intervention = Intervention(
            subject_person_id=enr.person_id,
            mentor_id=enr.mentor_id,
            rule=MISSED_RULE,
            reason=reason,
            status="open",
            due_at=moment + timedelta(days=PROCESS.intervention_sla_days),
        )
        session.add(intervention)
        await session.flush()

        await record_event(
            session,
            event_type=FLAG_RAISED,
            subject_person_id=enr.person_id,
            payload={"rule": MISSED_RULE, "reason": reason},
            evidence_ref=f"flag:{flag.id}",
            occurred_at=moment,
        )
        await record_event(
            session,
            event_type=INTERVENTION_CREATED,
            subject_person_id=enr.person_id,
            payload={"rule": MISSED_RULE, "mentor_id": enr.mentor_id},
            evidence_ref=f"intervention:{intervention.id}",
            occurred_at=moment,
        )
        raised.append(intervention)
    return raised


async def complete_intervention(
    session: AsyncSession,
    mentor: Person,
    intervention_id: int,
    outcome: str,
    now: datetime | None = None,
) -> Intervention | None:
    """Close an intervention with a recorded outcome."""
    moment = now or datetime.now(UTC)
    intervention = await session.get(Intervention, intervention_id)
    if intervention is None or intervention.status not in OPEN_STATES:
        return None
    intervention.status = "completed"
    intervention.completed_at = moment
    intervention.outcome = outcome
    await record_event(
        session,
        event_type=INTERVENTION_COMPLETED,
        subject_person_id=intervention.subject_person_id,
        actor_person_id=mentor.id,
        actor_role=Role.MENTOR,
        payload={"outcome": outcome},
        evidence_ref=f"intervention:{intervention.id}",
        occurred_at=moment,
    )
    return intervention


async def sweep_overdue(session: AsyncSession, now: datetime | None = None) -> int:
    """Mark open interventions past their SLA as overdue (once each)."""
    moment = now or datetime.now(UTC)
    due = (
        await session.execute(
            select(Intervention).where(
                Intervention.status == "open", Intervention.due_at < moment
            )
        )
    ).scalars().all()
    for intervention in due:
        intervention.status = "overdue"
        await record_event(
            session,
            event_type=INTERVENTION_OVERDUE,
            subject_person_id=intervention.subject_person_id,
            payload={"rule": intervention.rule},
            evidence_ref=f"intervention:{intervention.id}",
            occurred_at=moment,
        )
    return len(due)


# ── CEO weekly brief ─────────────────────────────────────────────────────────


async def weekly_brief(session: AsyncSession, now: datetime | None = None) -> dict[str, object]:
    """Assemble the executive brief from canonical data only.

    Every figure traces to a table. Where evidence is thin the brief says so
    rather than inventing a number.
    """
    moment = now or datetime.now(UTC)
    week = current_week_ending(moment)

    active_students = (
        await session.execute(
            select(func.count(func.distinct(Enrolment.person_id))).where(
                Enrolment.status == "active"
            )
        )
    ).scalar_one()

    submitted = (
        await session.execute(
            select(func.count()).select_from(FridayReport).where(
                FridayReport.week_ending == week
            )
        )
    ).scalar_one()

    open_interventions = (
        await session.execute(
            select(func.count()).select_from(Intervention).where(
                Intervention.status == "open"
            )
        )
    ).scalar_one()
    overdue_interventions = (
        await session.execute(
            select(func.count()).select_from(Intervention).where(
                Intervention.status == "overdue"
            )
        )
    ).scalar_one()

    # Dormant: no report in the last two completed weeks.
    dormant_weeks = recent_completed_weeks(2, moment)
    reported_ids = set(
        (
            await session.execute(
                select(FridayReport.person_id.distinct()).where(
                    FridayReport.week_ending.in_(dormant_weeks)
                )
            )
        ).scalars().all()
    )
    all_active_ids = set(
        (
            await session.execute(
                select(Enrolment.person_id.distinct()).where(Enrolment.status == "active")
            )
        ).scalars().all()
    )
    dormant = len(all_active_ids - reported_ids) if dormant_weeks else 0

    compliance = (submitted / active_students) if active_students else None
    return {
        "week_ending": week.isoformat(),
        "active_students": active_students,
        "reports_submitted": submitted,
        "report_compliance": compliance,
        "open_interventions": open_interventions,
        "overdue_interventions": overdue_interventions,
        "dormant_students": dormant,
        "note": None
        if active_students
        else "Insufficient data: no active enrolments on record yet.",
    }


def format_brief(brief: dict[str, object], academy: str) -> str:
    """Human-readable brief. Compliance shown only when it can be computed."""
    comp = brief["report_compliance"]
    comp_line = f"{comp:.0%}" if isinstance(comp, float) else "insufficient data"
    lines = [
        f"📋 {academy.upper()} — WEEKLY EXECUTIVE BRIEF",
        f"week ending {brief['week_ending']}",
        "",
        f"Active students          {brief['active_students']}",
        f"Friday reports in        {brief['reports_submitted']}",
        f"Report compliance        {comp_line}",
        f"Open interventions       {brief['open_interventions']}",
        f"Overdue interventions    {brief['overdue_interventions']}",
        f"Dormant students         {brief['dormant_students']}",
    ]
    if brief.get("note"):
        lines += ["", str(brief["note"])]
    lines += ["", "Every figure is drawn from canonical records."]
    return "\n".join(lines)


async def cohort_names(session: AsyncSession) -> dict[int, str]:
    rows = (await session.execute(select(Cohort.id, Cohort.name))).all()
    return {int(cid): name for cid, name in rows}
