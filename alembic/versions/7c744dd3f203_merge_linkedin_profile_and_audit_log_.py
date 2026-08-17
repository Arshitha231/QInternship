"""merge linkedin_profile and audit_log chain columns

Revision ID: 7c744dd3f203
Revises: d5b81c4e77a3, d8e4b1f6a930
Create Date: 2026-08-16 18:43:18.825622

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7c744dd3f203'
down_revision: Union[str, Sequence[str], None] = ('d5b81c4e77a3', 'd8e4b1f6a930')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
