"""The canonical domain model and the append-only event store.

One identity per person; one record per fact; everything else a view. The
``Event`` table is the system's truth — an immutable, attributable stream that
entities summarise and the CEO brief is assembled from. Nothing that appears in
a report is un-sourced.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, IdMixin, TimestampMixin


class Person(IdMixin, TimestampMixin, Base):
    """One canonical identity. May be student, staff, or both over time."""

    __tablename__ = "person"
    __table_args__ = (Index("ix_person_telegram", "telegram_id"),)

    # BigInteger: Telegram user ids exceed the 32-bit signed range.
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    full_name: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    phone: Mapped[str | None] = mapped_column(String(40))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Demo records are isolated from every production aggregate by invariant.
    is_demo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class RoleAssignment(IdMixin, TimestampMixin, Base):
    """A role granted to a person. Access derives from these, never from a chat."""

    __tablename__ = "role_assignment"
    __table_args__ = (UniqueConstraint("person_id", "role", name="uq_person_role"),)

    person_id: Mapped[int] = mapped_column(ForeignKey("person.id"), nullable=False)
    role: Mapped[str] = mapped_column(String(24), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class Cohort(IdMixin, TimestampMixin, Base):
    """A group of students moving through a programme together."""

    __tablename__ = "cohort"

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    programme: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    started_on: Mapped[date | None] = mapped_column(Date)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_demo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class Enrolment(IdMixin, TimestampMixin, Base):
    """A person's place in a cohort, and who mentors them there."""

    __tablename__ = "enrolment"
    __table_args__ = (UniqueConstraint("person_id", "cohort_id", name="uq_person_cohort"),)

    person_id: Mapped[int] = mapped_column(ForeignKey("person.id"), nullable=False)
    cohort_id: Mapped[int] = mapped_column(ForeignKey("cohort.id"), nullable=False)
    # TODO(founder Q3): confirm cohort→mentor assignment. One mentor per student
    # here; a shared model would move this to its own table.
    mentor_id: Mapped[int | None] = mapped_column(ForeignKey("person.id"))
    programme: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="active")


class Event(IdMixin, Base):
    """Append-only. The source of truth. Never updated, never deleted."""

    __tablename__ = "event"
    __table_args__ = (
        Index("ix_event_type", "event_type"),
        Index("ix_event_subject", "subject_person_id"),
        Index("ix_event_occurred", "occurred_at"),
    )

    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    # Who caused it. NULL actor means the SYSTEM raised it (a flag, an overdue).
    actor_person_id: Mapped[int | None] = mapped_column(ForeignKey("person.id"))
    actor_role: Mapped[str] = mapped_column(String(24), nullable=False, default="system")
    subject_person_id: Mapped[int | None] = mapped_column(ForeignKey("person.id"))
    payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    # A pointer to the evidence this event rests on (e.g. friday_report:123).
    evidence_ref: Mapped[str | None] = mapped_column(String(80))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class FridayReport(IdMixin, TimestampMixin, Base):
    """One student's structured weekly report. Immutable once submitted."""

    __tablename__ = "friday_report"
    __table_args__ = (
        UniqueConstraint("person_id", "week_ending", name="uq_report_week"),
        Index("ix_report_week", "week_ending"),
    )

    person_id: Mapped[int] = mapped_column(ForeignKey("person.id"), nullable=False)
    cohort_id: Mapped[int | None] = mapped_column(ForeignKey("cohort.id"))
    week_ending: Mapped[date] = mapped_column(Date, nullable=False)
    answers: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MentorReview(IdMixin, TimestampMixin, Base):
    """A mentor's review attached to a specific report — evidence, not a vibe."""

    __tablename__ = "mentor_review"

    report_id: Mapped[int] = mapped_column(ForeignKey("friday_report.id"), nullable=False)
    mentor_id: Mapped[int] = mapped_column(ForeignKey("person.id"), nullable=False)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")


class Flag(IdMixin, TimestampMixin, Base):
    """A raised concern that explains itself. Every flag names its rule."""

    __tablename__ = "flag"

    subject_person_id: Mapped[int] = mapped_column(ForeignKey("person.id"), nullable=False)
    rule: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    resolved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class Intervention(IdMixin, TimestampMixin, Base):
    """A mentor task raised for a student, tracked to an outcome or overdue."""

    __tablename__ = "intervention"
    __table_args__ = (Index("ix_intervention_status", "status"),)

    subject_person_id: Mapped[int] = mapped_column(ForeignKey("person.id"), nullable=False)
    mentor_id: Mapped[int | None] = mapped_column(ForeignKey("person.id"))
    rule: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str | None] = mapped_column(Text)


# ── Organisation model (Increment 2) ─────────────────────────────────────────
# Departments, staff profiles, reporting lines and schedules. All are
# data/config-driven (see app.org): the engine only ever asks about roles and
# these records, never about a person by name.


class Department(IdMixin, TimestampMixin, Base):
    """A unit of the company. Seeded from config; extendable by an admin."""

    __tablename__ = "department"

    code: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class StaffProfile(IdMixin, TimestampMixin, Base):
    """Staff-side attributes for a canonical Person. One per staff member."""

    __tablename__ = "staff_profile"
    __table_args__ = (
        UniqueConstraint("person_id", name="uq_staff_person"),
        Index("ix_staff_join_code", "join_code", unique=True),
    )

    person_id: Mapped[int] = mapped_column(ForeignKey("person.id"), nullable=False)
    department_id: Mapped[int | None] = mapped_column(ForeignKey("department.id"))
    title: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    # employment_type: OFFICE / REMOTE / HYBRID etc. — attendance policy hangs
    # off this later. TODO(founder): confirm each person's arrangement.
    employment_type: Mapped[str] = mapped_column(String(24), nullable=False, default="office")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_demo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # One-time code a staff member sends to link their Telegram (Slice 3.5).
    # Cleared once linked; NULLs are distinct so many can be unset at once.
    # Uniqueness is a unique index (see __table_args__) so it migrates on SQLite.
    join_code: Mapped[str | None] = mapped_column(String(12))
    # server_default so the migration can add this column to the already-
    # populated staff_profile table in production.
    linked: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )


class ReportingLine(IdMixin, TimestampMixin, Base):
    """Who a person reports to. One current manager per person.

    ``confirmed`` stays False until a founder approves the relationship — the
    system supports it structurally but never *infers* an unconfirmed line.
    """

    __tablename__ = "reporting_line"
    __table_args__ = (UniqueConstraint("person_id", name="uq_reporting_person"),)

    person_id: Mapped[int] = mapped_column(ForeignKey("person.id"), nullable=False)
    manager_id: Mapped[int] = mapped_column(ForeignKey("person.id"), nullable=False)
    confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class WorkSchedule(IdMixin, TimestampMixin, Base):
    """Expected working pattern — the baseline "who was expected" comes from here.

    Defaults are sensible placeholders; ``confirmed`` stays False until a
    founder signs off the real schedule, and attendance logic treats an
    unconfirmed schedule as advisory only.
    """

    __tablename__ = "work_schedule"
    __table_args__ = (UniqueConstraint("person_id", name="uq_schedule_person"),)

    person_id: Mapped[int] = mapped_column(ForeignKey("person.id"), nullable=False)
    timezone: Mapped[str] = mapped_column(String(48), nullable=False, default="Africa/Lagos")
    # ISO weekday numbers expected in office, Monday=1 … Sunday=7.
    workdays: Mapped[str] = mapped_column(String(16), nullable=False, default="1,2,3,4,5")
    start_local: Mapped[str] = mapped_column(String(5), nullable=False, default="09:00")
    end_local: Mapped[str] = mapped_column(String(5), nullable=False, default="17:00")
    confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


# Open, un-completed states an intervention can sit in.
OPEN_STATES: frozenset[str] = frozenset({"open", "overdue"})


# ── Truth & Verification layer (Increment 2 · Slice 2) ────────────────────────
# The reusable provenance spine. A Claim is what someone (or the system) says
# happened; Evidence supports it; a Verification is an independent check;
# an Exception records a conflict without accusing anyone. The original claim
# and its evidence are never mutated away — state moves forward, auditably.


class Claim(IdMixin, TimestampMixin, Base):
    """A stated fact and its position on the road from assertion to verified."""

    __tablename__ = "claim"
    __table_args__ = (
        Index("ix_claim_type", "claim_type"),
        Index("ix_claim_subject", "subject_person_id"),
        Index("ix_claim_state", "provenance_state"),
    )

    claim_type: Mapped[str] = mapped_column(String(64), nullable=False)
    # Who/what the claim is about, and who asserted it (NULL asserter = system).
    subject_person_id: Mapped[int | None] = mapped_column(ForeignKey("person.id"))
    asserted_by_person_id: Mapped[int | None] = mapped_column(ForeignKey("person.id"))
    asserted_role: Mapped[str] = mapped_column(String(24), nullable=False, default="system")
    # The claimed content itself.
    statement: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    # Provenance/verification, as strings/ints from app.truth.
    provenance_state: Mapped[str] = mapped_column(String(24), nullable=False)
    verification_level: Mapped[int] = mapped_column(nullable=False, default=0)
    # Snapshot of the strength this claim type required at creation (audit).
    required_level: Mapped[int] = mapped_column(nullable=False, default=0)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    evidence_ref: Mapped[str | None] = mapped_column(String(80))


class Evidence(IdMixin, TimestampMixin, Base):
    """A piece of evidence attached to a claim. Never deleted."""

    __tablename__ = "evidence"
    __table_args__ = (Index("ix_evidence_claim", "claim_id"),)

    claim_id: Mapped[int] = mapped_column(ForeignKey("claim.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # photo|link|telegram_file|…
    ref: Mapped[str] = mapped_column(String(400), nullable=False, default="")
    submitted_by_person_id: Mapped[int | None] = mapped_column(ForeignKey("person.id"))
    note: Mapped[str | None] = mapped_column(Text)


class Verification(IdMixin, TimestampMixin, Base):
    """An independent check of a claim and its outcome. Append-only."""

    __tablename__ = "verification"
    __table_args__ = (Index("ix_verification_claim", "claim_id"),)

    claim_id: Mapped[int] = mapped_column(ForeignKey("claim.id"), nullable=False)
    method: Mapped[str] = mapped_column(String(48), nullable=False)  # office_challenge|manual|…
    checked_by_person_id: Mapped[int | None] = mapped_column(ForeignKey("person.id"))
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)  # pass|fail|inconclusive
    level_reached: Mapped[int] = mapped_column(nullable=False, default=0)
    note: Mapped[str | None] = mapped_column(Text)


class VerificationException(IdMixin, TimestampMixin, Base):
    """A conflict around a claim — an exception to resolve, not an accusation.

    Preserves the original claim (never rewritten) and, on manual override, the
    prior state, actor, time and reason (recorded as events).
    """

    __tablename__ = "verification_exception"
    __table_args__ = (Index("ix_vexception_status", "status"),)

    claim_id: Mapped[int] = mapped_column(ForeignKey("claim.id"), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(48), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    reviewer_person_id: Mapped[int | None] = mapped_column(ForeignKey("person.id"))
    resolution: Mapped[str | None] = mapped_column(Text)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ── Staff attendance (Increment 2 · Slice 3) ──────────────────────────────────


class CheckIn(IdMixin, TimestampMixin, Base):
    """One attendance record per person per work day. The claim it links to
    (in the Truth Ledger) carries the provenance; ``verified`` is the cached
    view of whether the office challenge corroborated an OFFICE presence."""

    __tablename__ = "check_in"
    __table_args__ = (
        UniqueConstraint("person_id", "work_date", name="uq_checkin_person_day"),
        Index("ix_checkin_day", "work_date"),
    )

    person_id: Mapped[int] = mapped_column(ForeignKey("person.id"), nullable=False)
    work_date: Mapped[date] = mapped_column(Date, nullable=False)
    presence_type: Mapped[str] = mapped_column(String(16), nullable=False)
    claim_id: Mapped[int] = mapped_column(ForeignKey("claim.id"), nullable=False)
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
