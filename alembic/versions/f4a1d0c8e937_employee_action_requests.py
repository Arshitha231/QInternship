"""employee_action_requests

Revision ID: f4a1d0c8e937
Revises: e14c7a2f9b03
Create Date: 2026-08-18

New table only. Nothing existing changes.

Maker-checker staging for restrict/deactivate — app.writes.request_restriction
and request_deactivation create a row here instead of applying the change
directly; only approve_action_request, called by the row's own resolved
approver_id, actually flips availability_status or is_active.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "f4a1d0c8e937"
down_revision: Union[str, Sequence[str], None] = "e14c7a2f9b03"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "employee_action_requests",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("action_type", sa.String(length=20), nullable=False),
        sa.Column("target_employee_id", sa.String(length=36), sa.ForeignKey("employees.id"), nullable=False),
        sa.Column("requested_by", sa.String(length=36), nullable=False),
        sa.Column("approver_id", sa.String(length=36), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("resolved_by", sa.String(length=36), nullable=True),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_employee_action_requests_approver_status",
        "employee_action_requests", ["approver_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_employee_action_requests_approver_status", table_name="employee_action_requests")
    op.drop_table("employee_action_requests")
