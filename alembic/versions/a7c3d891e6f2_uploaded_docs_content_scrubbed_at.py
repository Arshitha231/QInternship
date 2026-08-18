"""uploaded_docs.content_scrubbed_at

Revision ID: a7c3d891e6f2
Revises: 9d4b45b07782
Create Date: 2026-08-17

One additive, nullable column. Nothing existing changes.

Marks the moment app.proposals.finalize_document wipes extracted_text after
every actionable proposed change from a document has been decided — the row
itself stays (proposed_changes/doc_subject_matches hold a non-nullable FK
into it, and those are never deleted), only the document's content goes.
NULL means "still has its extracted text" for every row that predates this
column, which is the correct reading — nothing gets scrubbed retroactively.

Originally chained off 1e1c372048fc directly (this branch's own head at the
time); retargeted onto 9d4b45b07782 once main's own migrations landed on top
of that same commit, so this doesn't reopen the dual-head split
9d4b45b07782 itself exists to close.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a7c3d891e6f2"
down_revision: Union[str, Sequence[str], None] = "9d4b45b07782"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("uploaded_docs", sa.Column("content_scrubbed_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("uploaded_docs", "content_scrubbed_at")
