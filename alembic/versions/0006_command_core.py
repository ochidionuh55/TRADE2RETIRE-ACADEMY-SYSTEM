"""T2R Command assignments and office correspondence.

Revision ID: 0006_command_core
Revises: 0005_daily_loop
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_command_core"
down_revision: str | None = "0005_daily_loop"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "assignment",
        sa.Column("created_by_person_id", sa.Integer(), nullable=False),
        sa.Column("assignee_person_id", sa.Integer(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("cadence", sa.String(length=16), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("completion_claim_id", sa.Integer(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["assignee_person_id"], ["person.id"]),
        sa.ForeignKeyConstraint(["completion_claim_id"], ["claim.id"]),
        sa.ForeignKeyConstraint(["created_by_person_id"], ["person.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_assignment_assignee_status", "assignment", ["assignee_person_id", "status"])
    op.create_index("ix_assignment_due", "assignment", ["due_at"])
    op.create_table(
        "assignment_history",
        sa.Column("assignment_id", sa.Integer(), nullable=False),
        sa.Column("actor_person_id", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(length=24), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["actor_person_id"], ["person.id"]),
        sa.ForeignKeyConstraint(["assignment_id"], ["assignment.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_assignment_history_assignment", "assignment_history", ["assignment_id"])
    op.create_table(
        "correspondence",
        sa.Column("sender_person_id", sa.Integer(), nullable=False),
        sa.Column("recipient_person_id", sa.Integer(), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("parent_id", sa.Integer(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["parent_id"], ["correspondence.id"]),
        sa.ForeignKeyConstraint(["recipient_person_id"], ["person.id"]),
        sa.ForeignKeyConstraint(["sender_person_id"], ["person.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_correspondence_recipient_status", "correspondence", ["recipient_person_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_correspondence_recipient_status", table_name="correspondence")
    op.drop_table("correspondence")
    op.drop_index("ix_assignment_history_assignment", table_name="assignment_history")
    op.drop_table("assignment_history")
    op.drop_index("ix_assignment_due", table_name="assignment")
    op.drop_index("ix_assignment_assignee_status", table_name="assignment")
    op.drop_table("assignment")
