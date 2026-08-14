"""it role, uploaded_docs, proposed_changes

Revision ID: 2995d6c4f6a9
Revises: 12c1545605b0
Create Date: 2026-08-14

THE "it" ROLE NEEDS NO SCHEMA CHANGE, and that is worth stating explicitly
rather than leaving as a silent omission. There is no role column on
employees and never has been: roles arrive per request, from a dev header or
an Entra app-role claim (app/auth.py), and app/config.py's hr_org_unit_name
documents the org tree as the fallback signal for the one context — a
scheduled sweep — that has no request to read a claim from. Adding "it"
was therefore three lines in app/auth.py (the Role literal, VALID_ROLES, and
the Entra role map) and there is no constraint, enum or lookup table here
that would have listed the old three. The ~30 people already seeded into the
IT division by seed_it_division.py are unaffected by this migration: they are
org-unit members, which is a different thing from role holders, and nothing
below touches employees' org structure.

What this migration actually adds:

  uploaded_docs      a parsed document. Stores the EXTRACTED TEXT, not the
                     original bytes — everything downstream works from text,
                     and this database has no other binary column and no
                     lifecycle policy for one. filename/content_type/byte_size
                     are kept so a reviewer can identify what they're looking
                     at

  proposed_changes   the staging table the whole extraction feature exists
                     for. employee_id is NULLABLE: name resolution genuinely
                     fails (the directory deliberately contains two people
                     called "Priya Sharma"), and an unresolved row is a real
                     reviewable outcome rather than an error. Nothing here is
                     searchable or indexed until status becomes accepted/edited

  employee_projects.contribution
                     Text, nullable. What someone DID, as prose, as opposed to
                     `role`, which is their title and String(150). Populated
                     only by an accepted proposed_change, so every existing
                     row keeps NULL

  audit_log.source   nullable String(50). "ai_extraction" for anything
                     committed out of proposed_changes, NULL otherwise.
                     Deliberately not backfilled to "direct": existing rows
                     must keep meaning exactly what they meant when written,
                     and inventing provenance is the one thing an audit table
                     must not do. Read NULL as "not recorded"

Both enum columns are native_enum=False with SQLAlchemy 2.x's default
create_constraint=False, matching every other enum in this schema — they are
plain VARCHARs, so no CHECK constraint is created and adding a status value
later is not a migration.

The enums are spelled out as sa.Enum(..., native_enum=False) rather than a
hand-sized sa.String, matching every other enum column in this schema and —
more usefully — keeping `alembic revision --autogenerate` silent. A bare
VARCHAR(20) here renders identically in SQLite and SQL Server but reports as
drift against the model on every future autogenerate run, which is how real
drift ends up being ignored.

Downgrade drops the two new tables and both new columns. Dropping a column
is destructive on the way down — accepted contributions and recorded
provenance are lost — which is correct for a downgrade but worth knowing
before running one against anything real.
"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '2995d6c4f6a9'
down_revision: Union[str, Sequence[str], None] = '12c1545605b0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'uploaded_docs',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('filename', sa.String(length=500), nullable=False),
        sa.Column('content_type', sa.String(length=150), nullable=False),
        sa.Column('byte_size', sa.Integer(), nullable=False),
        sa.Column('extracted_text', sa.Text(), nullable=False),
        sa.Column('uploaded_by', sa.String(length=36), nullable=False),
        sa.Column('uploaded_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_uploaded_docs')),
    )

    op.create_table(
        'proposed_changes',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        # Nullable until a human attributes it — see the module docstring.
        sa.Column('employee_id', sa.String(length=36), nullable=True),
        sa.Column('source_doc_id', sa.Integer(), nullable=False),
        sa.Column('field_type', sa.Enum(
            'project', 'skill', name='proposedfieldtype', native_enum=False), nullable=False),
        sa.Column('raw_extraction', sa.Text(), nullable=False),
        sa.Column('proposed_content', sa.Text(), nullable=False),
        sa.Column('confidence', sa.Float(), nullable=False, server_default='0'),
        sa.Column('status', sa.Enum(
            'pending', 'accepted', 'edited', 'rejected',
            name='proposedchangestatus', native_enum=False), nullable=False,
            server_default='pending'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        # Not a foreign key, matching audit_log.actor_id: who reviewed this
        # has to outlive their employee row.
        sa.Column('reviewed_by', sa.String(length=36), nullable=True),
        sa.Column('reviewed_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ['employee_id'], ['employees.id'],
            name=op.f('fk_proposed_changes_employee_id_employees')),
        sa.ForeignKeyConstraint(
            ['source_doc_id'], ['uploaded_docs.id'],
            name=op.f('fk_proposed_changes_source_doc_id_uploaded_docs')),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_proposed_changes')),
    )
    # The review queue is always read per-document and filtered by status;
    # without this the queue page table-scans once the table has a few
    # thousand rows in it.
    op.create_index(
        'ix_proposed_changes_doc_status', 'proposed_changes',
        ['source_doc_id', 'status'], unique=False)

    with op.batch_alter_table('employee_projects') as batch_op:
        batch_op.add_column(sa.Column('contribution', sa.Text(), nullable=True))

    with op.batch_alter_table('audit_log') as batch_op:
        batch_op.add_column(sa.Column('source', sa.String(length=50), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('audit_log') as batch_op:
        batch_op.drop_column('source')

    with op.batch_alter_table('employee_projects') as batch_op:
        batch_op.drop_column('contribution')

    op.drop_index('ix_proposed_changes_doc_status', table_name='proposed_changes')
    op.drop_table('proposed_changes')
    op.drop_table('uploaded_docs')
