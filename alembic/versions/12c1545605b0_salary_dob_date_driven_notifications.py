"""salary, date of birth, date-driven notifications

Revision ID: 12c1545605b0
Revises: 6886efd9b63d
Create Date: 2026-08-13

employees gains three nullable columns:

  salary            Numeric(12,2), not Float — money in binary floating point
                    accumulates rounding error, and that's painful to walk
                    back once exports depend on it
  salary_currency   ISO 4217. The dataset spans five countries; a bare number
                    would be actively misleading
  date_of_birth     full date, for HR records

All three are nullable because every existing row has no value for them and
there is no honest default: a placeholder salary or birth date is worse than
an absent one.

notifications changes shape for the date-driven triggers (birthday, work
anniversary), which aren't about a course at all:

  course_id         NOT NULL -> NULL. Stays a real foreign key rather than
                    becoming a polymorphic "subject type + id" pair the
                    database couldn't check
  event_key         identifies the real-world occurrence, e.g.
                    "birthday:2026-08-13:<employee id>", indexed. A daily
                    sweep gets run twice eventually — retried cron, restarted
                    container, someone checking it works — and this is what
                    makes the second run a no-op rather than a second message
  kind              VARCHAR(24) -> VARCHAR(25), because
                    "work_anniversary_reminder" is one character longer than
                    the longest existing value. A pure length widening: these
                    enums are native_enum=False and SQLAlchemy 2.x defaults
                    create_constraint=False, so there is no CHECK constraint
                    listing the old values that would reject the new ones
                    (verified against the built schema, not assumed)

SQLite can't ALTER most column properties in place, hence batch mode; on SQL
Server batch mode is a passthrough to the plain ALTERs, and altering
nullability on a column that participates in a foreign key is permitted there.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '12c1545605b0'
down_revision: Union[str, Sequence[str], None] = '6886efd9b63d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('employees', schema=None) as batch_op:
        batch_op.add_column(sa.Column('salary', sa.Numeric(precision=12, scale=2), nullable=True))
        batch_op.add_column(sa.Column('salary_currency', sa.String(length=3), nullable=True))
        batch_op.add_column(sa.Column('date_of_birth', sa.Date(), nullable=True))

    with op.batch_alter_table('notifications', schema=None) as batch_op:
        batch_op.add_column(sa.Column('event_key', sa.String(length=120), nullable=True))
        batch_op.alter_column('course_id',
               existing_type=sa.INTEGER(),
               nullable=True)
        batch_op.alter_column('kind',
               existing_type=sa.VARCHAR(length=24),
               type_=sa.Enum('employee_course_reminder', 'manager_course_report', 'birthday_reminder', 'work_anniversary_reminder', name='notificationkind', native_enum=False),
               existing_nullable=False)
        batch_op.create_index(batch_op.f('ix_notifications_event_key'), ['event_key'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('notifications', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_notifications_event_key'))
        batch_op.alter_column('kind',
               existing_type=sa.Enum('employee_course_reminder', 'manager_course_report', 'birthday_reminder', 'work_anniversary_reminder', name='notificationkind', native_enum=False),
               type_=sa.VARCHAR(length=24),
               existing_nullable=False)
        batch_op.alter_column('course_id',
               existing_type=sa.INTEGER(),
               nullable=False)
        batch_op.drop_column('event_key')

    with op.batch_alter_table('employees', schema=None) as batch_op:
        batch_op.drop_column('date_of_birth')
        batch_op.drop_column('salary_currency')
        batch_op.drop_column('salary')
