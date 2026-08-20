"""project embeddings: repack vectors as float16

Revision ID: 3d7eacab6582
Revises: b7d3e0a41c92
Create Date: 2026-08-19

A DATA migration, same channel as 9f31c0d7ae64: the deployed database is
only reachable inside Azure's network boundary, and the App Service already
runs `alembic upgrade head` at startup.

app/project_search.py's pack_vector()/unpack_vector() switched from raw
float32 (array('f')) to raw float16 (struct.pack("<Ne")) -- half the
storage per row, safe because project_search._semantic_ranking() only
sorts on the dot-product score and discards the value, so rank order is
what has to survive, not exact distances. See app/models/project_embedding.py
for the full rationale.

This migration re-packs every EXISTING row to match, entirely offline: no
embedding call, no network dependency, no cost. It reads each row's
float32 blob, decodes it, and re-encodes at float16 -- the same values
build_project_embeddings.py already wrote, just narrower. New rows written
after this deploys go straight to float16 via the application code; this
migration only has stale float32 rows to catch up.

Guarded by byte length (4 bytes/dim vs 2 bytes/dim) so it's safe to run
against a table that's already float16 -- a re-run, or a fresh database
seeded after the application code changed, both no-op cleanly.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "3d7eacab6582"
down_revision: Union[str, Sequence[str], None] = "b7d3e0a41c92"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


embeddings_table = sa.table(
    "project_embeddings",
    sa.column("project_id", sa.Integer),
    sa.column("dimensions", sa.Integer),
    sa.column("vector", sa.LargeBinary),
)


def _repack(fmt_in: str, fmt_out: str) -> None:
    import struct

    conn = op.get_bind()
    rows = conn.execute(
        sa.select(embeddings_table.c.project_id, embeddings_table.c.dimensions, embeddings_table.c.vector)
    ).fetchall()
    bytes_in = struct.calcsize(f"<1{fmt_in}")
    for project_id, dimensions, vector in rows:
        if len(vector) != dimensions * bytes_in:
            continue  # already at the target width (or a shape we don't recognise) -- leave it alone
        values = struct.unpack(f"<{dimensions}{fmt_in}", vector)
        conn.execute(
            embeddings_table.update()
            .where(embeddings_table.c.project_id == project_id)
            .values(vector=struct.pack(f"<{dimensions}{fmt_out}", *values))
        )


def upgrade() -> None:
    _repack(fmt_in="f", fmt_out="e")  # float32 -> float16


def downgrade() -> None:
    _repack(fmt_in="e", fmt_out="f")  # float16 -> float32 (widens back; rounding already happened, not recovered)
