"""merge continuity/project-skills and it-role/doc-extraction branches

Revision ID: 988e51b2d49c
Revises: 286ccf5ba50a, 2995d6c4f6a9
Create Date: 2026-08-14

No-op merge revision — both branches forked independently off
12c1545605b0 (one added work_authorization_records/is_client_engagement/
project_skill_requirements, the other added the it role's uploaded_docs/
proposed_changes tables and employee_projects.contribution) and neither
depends on the other's DDL. This just joins the two heads back into one
so `alembic upgrade head` has a single target again.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '988e51b2d49c'
down_revision: Union[str, Sequence[str], None] = ('286ccf5ba50a', '2995d6c4f6a9')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
