"""merge name pronunciation and multi-step-chain migration lines

Revision ID: 9d4b45b07782
Revises: 1e1c372048fc, 7c744dd3f203
Create Date: 2026-08-17 13:36:47.531687

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9d4b45b07782'
down_revision: Union[str, Sequence[str], None] = ('1e1c372048fc', '7c744dd3f203')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
