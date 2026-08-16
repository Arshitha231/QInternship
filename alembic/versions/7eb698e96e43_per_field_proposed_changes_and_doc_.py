"""per-field proposed changes and doc_subject_matches

Revision ID: 7eb698e96e43
Revises: abb9a2a3ab1e
Create Date: 2026-08-15

Rebuilds `proposed_changes` and adds `doc_subject_matches`, for the second
version of the doc-upload feature: per-field proposals (one row per skill,
per contribution line, per project entry — not one per employee or per
document) plus a dedicated disambiguation table, so a document mentioning
"John" among three Johns produces a ranked candidate list a human resolves,
never an auto-assigned employee_id.

THIS IS A DESTRUCTIVE REBUILD OF proposed_changes, not an in-place ALTER —
confirmed against the deployed database before writing this migration:
`GET /proposed_changes` on tempest34.azurewebsites.net returns zero groups.
No uploaded document has ever been accepted, edited, or rejected through the
first version of this feature in production, so there is no review history
to preserve. `uploaded_docs` itself is untouched (its shape didn't change);
only `proposed_changes` is dropped and recreated.

What changed and why:

  field_type (project|skill) -> change_type (skill|contribution|
  project_entry). The old shape bundled a project's name, role, and
  contribution prose into one "project" row; change_type splits contribution
  (EmployeeProject.contribution, prose) from project_entry (the membership
  itself: which project, what role, when) so a reviewer can accept "they
  were on this project" while rejecting an overreaching contribution
  sentence, or the reverse, instead of an all-or-nothing bundle.

  raw_extraction/proposed_content -> proposed_value/original_value.
  proposed_value is proposed_content's direct successor (what would commit
  on accept, rewritten by /edit and /reassign). original_value is new: a
  snapshot of the CURRENT real value at extraction time (null for a field
  that doesn't exist yet, e.g. a skill the person doesn't hold), so the
  review screen can render a diff without a second query racing the real
  row, which could have changed since extraction ran. raw_extraction (what
  the model literally emitted, verbatim, never rewritten) is not carried
  forward as a separate column -- it was primarily needed for /correct's
  re-extraction loop, which is superseded by direct /edit for this version;
  the model's raw output is still recoverable from doc_subject_matches'
  extracted_signals plus proposed_value's initial pre-edit state if needed.

  subject_match_id (new, nullable, FK doc_subject_matches.id). Every
  field-level proposal traces back to the disambiguation row for the person
  it's about, from the moment extraction creates it -- before anyone knows
  which employee_id (if any) that resolves to. employee_id itself stays
  nullable and is only ever populated by a human resolving (or reassigning)
  the linked doc_subject_matches row; extraction never sets it directly.

doc_subject_matches is new: one row per person MENTIONED in a document,
separate from the field-level proposed_changes rows it gates, because one
unresolved person can block many field-level proposals at once. Columns:
extracted_name, extracted_signals (json: email/phone/department, only keys
the document actually evidenced), candidate_employee_ids (json, RANKED list
-- email match highest, name-only lowest, department/team mention a weak
tiebreaker; never collapsed to a single auto-assigned id no matter how
confident), resolution_status (unresolved|resolved|new_hire_candidate --
new_hire_candidate is its own terminal state, not "resolved with a null
employee_id", so it provably means "a human looked and nobody plausible
exists" rather than "hasn't been looked at yet"), resolved_employee_id,
resolved_by, resolved_at.

Enums are sa.Enum(..., native_enum=False) matching every other enum in this
schema, so `alembic revision --autogenerate` stays silent (see
2995d6c4f6a9's own note on this -- confirmed clean the same way here).

Downgrade recreates the OLD proposed_changes shape (field_type/
raw_extraction/proposed_content, no subject_match_id) so `alembic downgrade`
leaves a structurally valid schema rather than nothing -- but, same as the
upgrade, this is a rebuild in both directions: no data survives either way,
and there was none to lose.
"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '7eb698e96e43'
down_revision: Union[str, Sequence[str], None] = 'abb9a2a3ab1e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index('ix_proposed_changes_doc_status', table_name='proposed_changes')
    op.drop_table('proposed_changes')

    op.create_table(
        'doc_subject_matches',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('source_doc_id', sa.Integer(), nullable=False),
        sa.Column('extracted_name', sa.String(length=200), nullable=False),
        sa.Column('extracted_signals', sa.Text(), nullable=False, server_default='{}'),
        sa.Column('candidate_employee_ids', sa.Text(), nullable=False, server_default='[]'),
        sa.Column('resolution_status', sa.Enum(
            'unresolved', 'resolved', 'new_hire_candidate',
            name='resolutionstatus', native_enum=False),
            nullable=False, server_default='unresolved'),
        sa.Column('resolved_employee_id', sa.String(length=36), nullable=True),
        sa.Column('resolved_by', sa.String(length=36), nullable=True),
        sa.Column('resolved_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ['source_doc_id'], ['uploaded_docs.id'],
            name=op.f('fk_doc_subject_matches_source_doc_id_uploaded_docs')),
        sa.ForeignKeyConstraint(
            ['resolved_employee_id'], ['employees.id'],
            name=op.f('fk_doc_subject_matches_resolved_employee_id_employees')),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_doc_subject_matches')),
    )
    op.create_index(
        'ix_doc_subject_matches_doc_status', 'doc_subject_matches',
        ['source_doc_id', 'resolution_status'], unique=False)

    op.create_table(
        'proposed_changes',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('employee_id', sa.String(length=36), nullable=True),
        sa.Column('subject_match_id', sa.Integer(), nullable=True),
        sa.Column('source_doc_id', sa.Integer(), nullable=False),
        sa.Column('change_type', sa.Enum(
            'skill', 'contribution', 'project_entry',
            name='changetype', native_enum=False), nullable=False),
        sa.Column('proposed_value', sa.Text(), nullable=False),
        sa.Column('original_value', sa.Text(), nullable=True),
        sa.Column('confidence', sa.Float(), nullable=False, server_default='0'),
        sa.Column('status', sa.Enum(
            'pending', 'accepted', 'edited', 'rejected',
            name='proposedchangestatus', native_enum=False),
            nullable=False, server_default='pending'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('reviewed_by', sa.String(length=36), nullable=True),
        sa.Column('reviewed_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ['employee_id'], ['employees.id'],
            name=op.f('fk_proposed_changes_employee_id_employees')),
        sa.ForeignKeyConstraint(
            ['subject_match_id'], ['doc_subject_matches.id'],
            name=op.f('fk_proposed_changes_subject_match_id_doc_subject_matches')),
        sa.ForeignKeyConstraint(
            ['source_doc_id'], ['uploaded_docs.id'],
            name=op.f('fk_proposed_changes_source_doc_id_uploaded_docs')),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_proposed_changes')),
    )
    op.create_index(
        'ix_proposed_changes_doc_status', 'proposed_changes',
        ['source_doc_id', 'status'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_proposed_changes_doc_status', table_name='proposed_changes')
    op.drop_table('proposed_changes')

    op.drop_index('ix_doc_subject_matches_doc_status', table_name='doc_subject_matches')
    op.drop_table('doc_subject_matches')

    op.create_table(
        'proposed_changes',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('employee_id', sa.String(length=36), nullable=True),
        sa.Column('source_doc_id', sa.Integer(), nullable=False),
        sa.Column('field_type', sa.Enum(
            'project', 'skill', name='proposedfieldtype', native_enum=False), nullable=False),
        sa.Column('raw_extraction', sa.Text(), nullable=False),
        sa.Column('proposed_content', sa.Text(), nullable=False),
        sa.Column('confidence', sa.Float(), nullable=False, server_default='0'),
        sa.Column('status', sa.Enum(
            'pending', 'accepted', 'edited', 'rejected',
            name='proposedchangestatus', native_enum=False),
            nullable=False, server_default='pending'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
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
    op.create_index(
        'ix_proposed_changes_doc_status', 'proposed_changes',
        ['source_doc_id', 'status'], unique=False)
