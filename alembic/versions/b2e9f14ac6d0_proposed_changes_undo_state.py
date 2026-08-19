"""proposed_changes.undo_state

Revision ID: b2e9f14ac6d0
Revises: a7c3d891e6f2
Create Date: 2026-08-17

One additive, nullable column. Nothing existing changes.

Captures exactly what accept()/edit() wrote to EmployeeSkill/EmployeeProject
at the moment it wrote it, so app.proposals.undo() can reverse precisely
that effect rather than re-deriving it later against a state that may have
moved on. NULL for every row that predates this column (they were committed
before undo_state existed) and for any row once undo() has consumed it —
both read as "nothing recorded to reverse."
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b2e9f14ac6d0"
down_revision: Union[str, Sequence[str], None] = "a7c3d891e6f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("proposed_changes", sa.Column("undo_state", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("proposed_changes", "undo_state")
