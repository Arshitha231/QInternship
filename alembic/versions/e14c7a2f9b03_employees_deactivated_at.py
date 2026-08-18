"""employees.deactivated_at

Revision ID: e14c7a2f9b03
Revises: b2e9f14ac6d0
Create Date: 2026-08-18

One additive, nullable column. Nothing existing changes.

Set by app.writes.deactivate_employee, cleared by reactivate_employee.
is_active alone can't answer "when" -- and once is_active is False,
GET /people/{id} returns None for every caller including HR, so there is
no other read path left that could recover the timing after the fact.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "e14c7a2f9b03"
down_revision: Union[str, Sequence[str], None] = "b2e9f14ac6d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("employees", sa.Column("deactivated_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("employees", "deactivated_at")
