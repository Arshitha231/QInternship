"""project embeddings (Mode 3 semantic project search)

Revision ID: c4a7e1f93b02
Revises: f2b27b09b4a8
Create Date: 2026-08-15

One additive table, project_embeddings -- the retrieval index behind
app/project_search.py. Nothing existing changes.

Deliberately a table in our own database rather than a second Azure AI
Search index: at 128 projects brute-force cosine is sub-millisecond, the
team's Search allocation stays at one index, and local/deployed behaviour
stays identical. See app/models/project_embedding.py for the full
reasoning.

vector is LargeBinary (BLOB on SQLite, VARBINARY(max) on Azure SQL) holding
raw little-endian float32 -- 6 KB for a 1536-dim vector, versus ~30 KB as
JSON text. No dialect-specific DDL is needed for that on either backend.

No data is written here. Populating the table is an operator step
(build_project_embeddings.py), the same shape as build_search_index.py:
it needs the embedding endpoint's credentials and must not be part of a
migration that runs at app startup.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c4a7e1f93b02'
down_revision: Union[str, Sequence[str], None] = 'f2b27b09b4a8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'project_embeddings',
        sa.Column('project_id', sa.Integer(), nullable=False),
        sa.Column('model', sa.String(length=100), nullable=False),
        sa.Column('dimensions', sa.Integer(), nullable=False),
        sa.Column('source_hash', sa.String(length=64), nullable=False),
        sa.Column('vector', sa.LargeBinary(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ),
        sa.PrimaryKeyConstraint('project_id'),
    )


def downgrade() -> None:
    op.drop_table('project_embeddings')
