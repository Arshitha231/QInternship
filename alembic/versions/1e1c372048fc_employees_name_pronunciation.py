"""employees.name_pronunciation

Revision ID: 1e1c372048fc
Revises: c2efc0453802
Create Date: 2026-08-16

One additive, nullable column. Nothing existing changes.

Same shape as d5b81c4e77a3 (linkedin_profile), including the same mistake
that migration exists to warn against: adding a column to the ORM model,
schemas, permissions and the React form is not the same as adding it to the
database. This migration is the part of the change that actually reaches
Azure SQL.

Nullable, no server_default: the vast majority of existing rows have no
phonetic spelling on file yet. Short (200 chars, same cap as preferred_name)
because a phonetic respelling like "nuh-VAY-uh" is a few syllables, not a
paragraph.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "1e1c372048fc"
down_revision: Union[str, Sequence[str], None] = "c2efc0453802"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("employees", sa.Column("name_pronunciation", sa.String(length=200), nullable=True))


def downgrade() -> None:
    op.drop_column("employees", "name_pronunciation")
