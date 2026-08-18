"""employee_action_requests: nullable target + payload, for create requests

Creating an employee became a two-person action (app.models.enums.
EmployeeActionType.create), and a create request is the one shape that has
no employee to point at yet — so target_employee_id has to be nullable, and
the proposed fields need somewhere to sit until the approval lands.

batch_alter_table for the nullability change: SQLite can't ALTER a column,
so Alembic rebuilds the table. Nullable is a widening change, so existing
restrict/deactivate rows are unaffected either way, and the downgrade only
works while no create requests exist — which is exactly why it asserts
rather than silently dropping them.

Revision ID: c8f2b6d41a75
Revises: f4a1d0c8e937
"""
from alembic import op
import sqlalchemy as sa

revision = "c8f2b6d41a75"
down_revision = "f4a1d0c8e937"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("employee_action_requests") as batch_op:
        batch_op.alter_column(
            "target_employee_id", existing_type=sa.String(length=36), nullable=True,
        )
        batch_op.add_column(sa.Column("payload", sa.Text(), nullable=True))


def downgrade() -> None:
    # A create request has no target_employee_id to restore, so narrowing
    # the column back would fail on the NOT NULL constraint anyway. Failing
    # loudly here says why, instead of leaving an operator to decode a
    # constraint violation.
    pending = op.get_bind().execute(
        sa.text("SELECT COUNT(*) FROM employee_action_requests WHERE target_employee_id IS NULL")
    ).scalar()
    if pending:
        raise RuntimeError(
            f"{pending} create request(s) have no target_employee_id — resolve or delete them "
            "before downgrading, since this revision is what makes them representable."
        )

    with op.batch_alter_table("employee_action_requests") as batch_op:
        batch_op.drop_column("payload")
        batch_op.alter_column(
            "target_employee_id", existing_type=sa.String(length=36), nullable=False,
        )
