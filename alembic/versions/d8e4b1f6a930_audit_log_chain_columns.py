"""audit_log chain_id/chain_step columns

Revision ID: d8e4b1f6a930
Revises: 9f31c0d7ae64
Create Date: 2026-08-16 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'd8e4b1f6a930'
down_revision = '9f31c0d7ae64'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('audit_log') as batch_op:
        batch_op.add_column(sa.Column('chain_id', sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column('chain_step', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('audit_log') as batch_op:
        batch_op.drop_column('chain_step')
        batch_op.drop_column('chain_id')
