"""directory_object_id: unique constraint -> filtered unique index

Revision ID: a3f0c9d2e1b4
Revises: b01229e560ec
Create Date: 2026-08-11

A plain UNIQUE constraint on a nullable column means different things on
different databases: SQLite and Postgres treat NULL as "unknown", so any
number of NULL rows are allowed; SQL Server treats NULL as a comparable
value, so a UNIQUE constraint only allows a single NULL row. Every synthetic
employee without a linked Microsoft Graph identity has NULL here (see
app/models/employee.py), so the old constraint broke the very first seed run
against Azure SQL. A filtered/partial index -- unique only where the column
IS NOT NULL -- behaves identically on every backend.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a3f0c9d2e1b4'
down_revision: Union[str, Sequence[str], None] = 'b01229e560ec'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # SQLite can't ALTER a constraint off a live table -- batch mode does
    # the copy-and-move dance for it. On every other dialect (including
    # SQL Server) batch mode is a passthrough to the plain ALTER below.
    with op.batch_alter_table('employees') as batch_op:
        batch_op.drop_constraint(op.f('uq_employees_directory_object_id'), type_='unique')
    op.create_index(
        'ix_employees_directory_object_id', 'employees', ['directory_object_id'], unique=True,
        mssql_where=sa.text('directory_object_id IS NOT NULL'),
        sqlite_where=sa.text('directory_object_id IS NOT NULL'),
        postgresql_where=sa.text('directory_object_id IS NOT NULL'),
    )


def downgrade() -> None:
    op.drop_index('ix_employees_directory_object_id', table_name='employees')
    with op.batch_alter_table('employees') as batch_op:
        batch_op.create_unique_constraint(
            op.f('uq_employees_directory_object_id'), ['directory_object_id'],
        )
