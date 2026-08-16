"""merge doc-review pipeline and audit_log.routed_via branches

Revision ID: f2b27b09b4a8
Revises: 7eb698e96e43, 3d42371ad750
Create Date: 2026-08-15 19:05:03.132025

Both branches forked from the same parent (abb9a2a3ab1e) and touch disjoint
tables -- 7eb698e96e43 rebuilds proposed_changes and adds
doc_subject_matches, 3d42371ad750 adds one nullable column to audit_log --
so there's nothing to reconcile, same shape as 988e51b2d49c's precedent.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f2b27b09b4a8'
down_revision: Union[str, Sequence[str], None] = ('7eb698e96e43', '3d42371ad750')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
