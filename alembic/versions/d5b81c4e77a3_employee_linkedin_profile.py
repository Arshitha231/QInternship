"""employees.linkedin_profile

Revision ID: d5b81c4e77a3
Revises: 9f31c0d7ae64
Create Date: 2026-08-16

One additive, nullable column. Nothing existing changes.

THIS MIGRATION IS THE POINT. The column was proposed across six separate
PRs (#42-#47) that added it to the ORM model, the Pydantic schemas,
permissions, the registry and the React form -- but not to the database.
Every one of those PRs merges cleanly and the full suite passes, because
tests build their schema from the models with create_all(), so SQLite
always has the column. Azure SQL does not, and the first employee query
after deploy fails outright:

    (pymssql) Invalid column name 'linkedin_profile'
    -- locally reproducible as: no such column: employees.linkedin_profile

That is not a degraded feature, it is every /people, /search and /employees
request 500ing. An ORM column with no migration is indistinguishable from a
correct change until it reaches a database nobody re-created from models.

Nullable with no server_default on purpose: 530 existing rows have no
LinkedIn URL, and NULL is the honest representation of "we don't know it",
distinct from an empty string meaning "they don't have one". The read path
(app/people.py _build_detail) only sets the key when the field is visible,
and the routes serialize with exclude_unset, so a NULL is simply absent
from the response rather than surfacing as null.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "d5b81c4e77a3"
down_revision: Union[str, Sequence[str], None] = "9f31c0d7ae64"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("employees", sa.Column("linkedin_profile", sa.String(length=500), nullable=True))


def downgrade() -> None:
    op.drop_column("employees", "linkedin_profile")
