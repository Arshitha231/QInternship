"""merge acknowledgement and employee-action-request migration lines

Revision ID: 0e21182a9161
Revises: 2205be925fa2, c8f2b6d41a75
Create Date: 2026-08-18 17:24:11.956608

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0e21182a9161'
down_revision: Union[str, Sequence[str], None] = ('2205be925fa2', 'c8f2b6d41a75')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
