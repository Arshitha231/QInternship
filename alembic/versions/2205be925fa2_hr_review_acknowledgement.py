"""hr review acknowledgement

Revision ID: 2205be925fa2
Revises: 9d4b45b07782
Create Date: 2026-08-18

work_authorization_records gains two nullable columns:

  hr_review_acknowledged_at   when HR silenced the upcoming-review reminder
                               for this record's current next_hr_review_date
  hr_review_acknowledged_by   who did it (employee id, not a strict FK
                               enforced beyond the column type — mirrors
                               verified_by immediately above it)

Deliberately not the same thing as performing the review: doing the review
means superseding this row with a new verified one (new authorization_type /
next_hr_review_date), which starts unacknowledged on its own. These two
columns only ever gate app/notifications.py's upcoming-review sweep; they are
never read by GET /continuity/review-queue, which keeps listing a record for
as long as it's actually due regardless of acknowledgement.

Both nullable for the same reason every other optional column on this table
is: every existing row has never been acknowledged, and there is no honest
default besides absent.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2205be925fa2'
down_revision: Union[str, Sequence[str], None] = '9d4b45b07782'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('work_authorization_records', schema=None) as batch_op:
        batch_op.add_column(sa.Column('hr_review_acknowledged_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('hr_review_acknowledged_by', sa.String(length=36), nullable=True))
        batch_op.create_foreign_key(
            batch_op.f('fk_work_authorization_records_hr_review_acknowledged_by_employees'),
            'employees', ['hr_review_acknowledged_by'], ['id'],
        )


def downgrade() -> None:
    with op.batch_alter_table('work_authorization_records', schema=None) as batch_op:
        batch_op.drop_constraint(
            batch_op.f('fk_work_authorization_records_hr_review_acknowledged_by_employees'),
            type_='foreignkey',
        )
        batch_op.drop_column('hr_review_acknowledged_by')
        batch_op.drop_column('hr_review_acknowledged_at')
