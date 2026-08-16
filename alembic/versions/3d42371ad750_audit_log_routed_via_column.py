"""audit_log routed_via column

Revision ID: 3d42371ad750
Revises: abb9a2a3ab1e
Create Date: 2026-08-15

Piece 2 (the model-emitted PeopleQuery path): app.tool_calling's own
assistant-level audit row (action="assistant", distinct from each service
function's own row) now records which of "deterministic" /
"llm_fixed_tool" / "llm_plan_tool" / "last_resort_fallback" / "direct"
resolved the request -- so the reasoning_effort question for the new
search_people tool (does "minimal" reliably produce valid PeopleQuery
objects, or does that one tool need to step up) can be answered from real
logged failure rates instead of impressions during testing.

Nullable, same precedent as audit_log.source (2995d6c4f6a9): existing rows
predate this column and never claimed a routing path, so they stay null
("not recorded") rather than being backfilled with a guess.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3d42371ad750'
down_revision: Union[str, Sequence[str], None] = 'abb9a2a3ab1e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('audit_log') as batch_op:
        batch_op.add_column(sa.Column('routed_via', sa.String(length=50), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('audit_log') as batch_op:
        batch_op.drop_column('routed_via')
