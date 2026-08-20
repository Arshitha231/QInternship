"""course_requirement due dates

Adds the company-side deadline to a course requirement, so "overdue" and
"due soon" become derivable facts instead of guesses. Before this, the only
dates in the training tables were `attempted_at` and `completed_at` —
both reported by the training system about what someone DID, with nothing
anywhere saying when they were supposed to have done it. The HR and manager
dashboards need that second half.

Two nullable columns rather than one (see the model's own comment for the
full reasoning): `due_date` is a fixed org-wide deadline, and
`due_days_after_hire` a rolling per-person one resolved against
Employee.hire_date. Both NULL — the default for every existing row — means
the requirement has no deadline and can never be overdue, which is why this
migration needs no backfill: it is behaviour-preserving on data that
predates it.

Revision ID: d1a4f7b25c60
Revises: c8f2b6d41a75
Create Date: 2026-08-19
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d1a4f7b25c60"
down_revision: Union[str, Sequence[str], None] = "c8f2b6d41a75"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("course_requirements", sa.Column("due_date", sa.Date(), nullable=True))
    op.add_column("course_requirements", sa.Column("due_days_after_hire", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("course_requirements", "due_days_after_hire")
    op.drop_column("course_requirements", "due_date")
