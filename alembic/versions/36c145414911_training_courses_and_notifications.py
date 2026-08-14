"""training course status, requirements, notifications

Revision ID: 36c145414911
Revises: a3f0c9d2e1b4
Create Date: 2026-08-13

Four new tables, no changes to any existing one — certification tracking is
additive, and in particular does NOT touch employee_certifications, which
stays what it always was (self-entered credentials with an issuer and an
expiry). Externally-reported course progress is a different thing with a
different source of truth, so it gets its own table rather than overloading
that one with nullable status columns that would be meaningless on every
existing row.

  training_courses           course catalogue; `code` is the external join
                             key with the other team's training system
  course_requirements        which courses are expected of whom (org unit
                             subtree / job-title keyword / employment type)
  employee_course_statuses   per employee, per course: the four-value status,
                             attempt and completion dates, and provenance
  notifications              what was raised and to whom, with the explicit
                             `sequence` that pins employee-before-management
                             ordering rather than leaving it to clock ties

The four-value status is stored as a non-native (VARCHAR + CHECK) enum, same
as every other enum in this schema — SQLite has no enum type and Azure SQL's
would need a separate CREATE TYPE, so `native_enum=False` keeps one identical
DDL across both backends.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '36c145414911'
down_revision: Union[str, Sequence[str], None] = 'a3f0c9d2e1b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('training_courses',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('code', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_training_courses')),
    sa.UniqueConstraint('code', name=op.f('uq_training_courses_code'))
    )
    op.create_table('course_requirements',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('course_id', sa.Integer(), nullable=False),
    sa.Column('org_unit_id', sa.Integer(), nullable=True),
    sa.Column('job_title_keyword', sa.String(length=200), nullable=True),
    sa.Column('employment_type', sa.Enum('fte', 'contractor', 'intern', name='employmenttype', native_enum=False), nullable=True),
    sa.Column('note', sa.String(length=500), nullable=True),
    sa.ForeignKeyConstraint(['course_id'], ['training_courses.id'], name=op.f('fk_course_requirements_course_id_training_courses')),
    sa.ForeignKeyConstraint(['org_unit_id'], ['org_units.id'], name=op.f('fk_course_requirements_org_unit_id_org_units')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_course_requirements'))
    )
    op.create_table('employee_course_statuses',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('employee_id', sa.String(length=36), nullable=False),
    sa.Column('course_id', sa.Integer(), nullable=False),
    sa.Column('status', sa.Enum('not_started', 'in_progress', 'failed', 'completed', name='coursestatus', native_enum=False), nullable=False),
    sa.Column('attempted_at', sa.Date(), nullable=True),
    sa.Column('completed_at', sa.Date(), nullable=True),
    sa.Column('source', sa.String(length=50), nullable=False),
    sa.Column('last_synced_at', sa.DateTime(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['course_id'], ['training_courses.id'], name=op.f('fk_employee_course_statuses_course_id_training_courses')),
    sa.ForeignKeyConstraint(['employee_id'], ['employees.id'], name=op.f('fk_employee_course_statuses_employee_id_employees')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_employee_course_statuses')),
    sa.UniqueConstraint('employee_id', 'course_id', name=op.f('uq_employee_course_statuses_employee_id'))
    )
    op.create_table('notifications',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('recipient_id', sa.String(length=36), nullable=False),
    sa.Column('subject_employee_id', sa.String(length=36), nullable=False),
    sa.Column('course_id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.Enum('employee_course_reminder', 'manager_course_report', name='notificationkind', native_enum=False), nullable=False),
    sa.Column('display_status', sa.String(length=20), nullable=False),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('sequence', sa.Integer(), nullable=False),
    sa.Column('levels_up', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['course_id'], ['training_courses.id'], name=op.f('fk_notifications_course_id_training_courses')),
    sa.ForeignKeyConstraint(['recipient_id'], ['employees.id'], name=op.f('fk_notifications_recipient_id_employees')),
    sa.ForeignKeyConstraint(['subject_employee_id'], ['employees.id'], name=op.f('fk_notifications_subject_employee_id_employees')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_notifications'))
    )
    with op.batch_alter_table('notifications', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_notifications_recipient_id'), ['recipient_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('notifications', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_notifications_recipient_id'))

    op.drop_table('notifications')
    op.drop_table('employee_course_statuses')
    op.drop_table('course_requirements')
    op.drop_table('training_courses')
