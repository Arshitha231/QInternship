"""work authorization records, client-engagement flag

Revision ID: 0d944baf8490
Revises: 12c1545605b0
Create Date: 2026-08-14

Two additive changes for the continuity feature (Phase 4):

  work_authorization_records   New table, HR-verified work-authorization
                                history per employee. A row per status, not
                                two columns on employees -- HR's real pain
                                point is tracking changes (CPT -> OPT ->
                                STEM OPT -> ...), not one current expiry
                                date. A filtered unique index enforces at
                                most one is_current=True row per employee,
                                identically across SQLite/Azure SQL, same
                                pattern as employees.directory_object_id's
                                own filtered index (migration a3f0c9d2e1b4).

                                Deliberately outside app/registry.py's
                                REGISTRY -- never joined into find_people/
                                PeopleQuery, so it's structurally
                                unreachable through the general query
                                pipeline, not merely policy-blocked. See
                                app/registry.py's module docstring.

  projects.is_client_engagement  One new boolean column. Confirmed against
                                the seed data that Project.type=="project"
                                is not a usable proxy for "client
                                engagement" -- those rows are internal
                                operational work ("Payroll Onboarding
                                Revamp"), not client-facing -- so this is a
                                real signal, not a derivable one.
                                server_default=false because the 126
                                existing rows need a real value, not NULL.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0d944baf8490'
down_revision: Union[str, Sequence[str], None] = '12c1545605b0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('work_authorization_records',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('employee_id', sa.String(length=36), nullable=False),
    sa.Column('authorization_type', sa.Enum('citizen', 'permanent_resident', 'cpt', 'opt', 'stem_opt', 'h1b', 'l1', 'other', name='workauthorizationtype', native_enum=False), nullable=False),
    sa.Column('effective_from', sa.Date(), nullable=False),
    sa.Column('effective_until', sa.Date(), nullable=True),
    sa.Column('next_hr_review_date', sa.Date(), nullable=True),
    sa.Column('verification_status', sa.Enum('pending_verification', 'verified', 'superseded', 'expired_record', name='verificationstatus', native_enum=False), nullable=False),
    sa.Column('verified_at', sa.DateTime(), nullable=True),
    sa.Column('verified_by', sa.String(length=36), nullable=True),
    sa.Column('is_current', sa.Boolean(), nullable=False),
    sa.Column('supersedes_record_id', sa.Integer(), nullable=True),
    sa.Column('source_document_type', sa.String(length=100), nullable=True),
    sa.Column('internal_notes', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['employee_id'], ['employees.id'], name=op.f('fk_work_authorization_records_employee_id_employees')),
    sa.ForeignKeyConstraint(['supersedes_record_id'], ['work_authorization_records.id'], name=op.f('fk_work_authorization_records_supersedes_record_id_work_authorization_records')),
    sa.ForeignKeyConstraint(['verified_by'], ['employees.id'], name=op.f('fk_work_authorization_records_verified_by_employees')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_work_authorization_records'))
    )
    op.create_index(
        'ix_work_authorization_records_employee_current', 'work_authorization_records', ['employee_id'], unique=True,
        mssql_where=sa.text('is_current = 1'),
        sqlite_where=sa.text('is_current = 1'),
        postgresql_where=sa.text('is_current = true'),
    )

    with op.batch_alter_table('projects', schema=None) as batch_op:
        batch_op.add_column(sa.Column('is_client_engagement', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    with op.batch_alter_table('projects', schema=None) as batch_op:
        batch_op.drop_column('is_client_engagement')

    op.drop_index('ix_work_authorization_records_employee_current', table_name='work_authorization_records')
    op.drop_table('work_authorization_records')
