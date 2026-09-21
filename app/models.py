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
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, IdMixin, TimestampMixin


class Person(IdMixin, TimestampMixin, Base):
    """One canonical identity. May be student, staff, or both over time."""

    __tablename__ = "person"
    __table_args__ = (Index("ix_person_telegram", "telegram_id"),)

    telegram_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    full_name: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    phone: Mapped[str | None] = mapped_column(String(40))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


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


# Open, un-completed states an intervention can sit in.
OPEN_STATES: frozenset[str] = frozenset({"open", "overdue"})
