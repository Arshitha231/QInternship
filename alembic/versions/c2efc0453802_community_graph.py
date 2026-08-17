"""community graph

Revision ID: c2efc0453802
Revises: d5b81c4e77a3
Create Date: 2026-08-16

Adds the Community Graph feature: each employee's private "who to contact
for what" list, separate from the org/team/skills graphs already in this
schema.

  community_links        One edge per (owner, contact) — official
                          (HR-bootstrapped, read-only to the employee) or
                          personal (the employee's own addition, requiring
                          a reason). Visibility is unconditionally private
                          to owner_employee_id; see app/community_links.py.
                          is_mentor_link marks the subset of official links
                          whose expiration is computed at read time from
                          org_settings rather than stored as a fixed date.

  org_settings            HR-configurable mentor_link_duration_days,
                          org-wide (office_id NULL) or per-office. Same
                          filtered-unique-index pattern as
                          employees.directory_object_id, for the same
                          SQLite/Azure SQL NULL-uniqueness portability
                          reason.

  suggested_official_links  A staging table for HR to review office/role ->
                          candidate mappings bootstrapped from existing
                          office/job-title data, mirroring
                          proposed_changes' staging discipline — nothing
                          here is a real community_links edge until an
                          explicit confirm.

Enums are sa.Enum(..., native_enum=False), matching every other enum in
this schema.
"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c2efc0453802'
down_revision: Union[str, Sequence[str], None] = 'd5b81c4e77a3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'org_settings',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('office_id', sa.Integer(), nullable=True),
        sa.Column('mentor_link_duration_days', sa.Integer(), nullable=False, server_default='90'),
        sa.ForeignKeyConstraint(
            ['office_id'], ['offices.id'], name=op.f('fk_org_settings_office_id_offices')),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_org_settings')),
    )
    op.create_index(
        'ix_org_settings_office_id', 'org_settings', ['office_id'], unique=True,
        sqlite_where=sa.text('office_id IS NOT NULL'),
        mssql_where=sa.text('office_id IS NOT NULL'),
        postgresql_where=sa.text('office_id IS NOT NULL'),
    )

    op.create_table(
        'community_links',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('owner_employee_id', sa.String(length=36), nullable=False),
        sa.Column('contact_employee_id', sa.String(length=36), nullable=False),
        sa.Column('role_label', sa.String(length=200), nullable=False),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('source', sa.Enum(
            'official', 'personal', name='communitylinksource', native_enum=False), nullable=False),
        sa.Column('office_id', sa.Integer(), nullable=True),
        sa.Column('department_id', sa.Integer(), nullable=True),
        sa.Column('is_mentor_link', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ['owner_employee_id'], ['employees.id'],
            name=op.f('fk_community_links_owner_employee_id_employees')),
        sa.ForeignKeyConstraint(
            ['contact_employee_id'], ['employees.id'],
            name=op.f('fk_community_links_contact_employee_id_employees')),
        sa.ForeignKeyConstraint(
            ['office_id'], ['offices.id'], name=op.f('fk_community_links_office_id_offices')),
        sa.ForeignKeyConstraint(
            ['department_id'], ['org_units.id'], name=op.f('fk_community_links_department_id_org_units')),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_community_links')),
    )
    op.create_index(
        'ix_community_links_owner', 'community_links', ['owner_employee_id'], unique=False)

    op.create_table(
        'suggested_official_links',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('office_id', sa.Integer(), nullable=False),
        sa.Column('role_label', sa.String(length=200), nullable=False),
        sa.Column('candidate_employee_id', sa.String(length=36), nullable=False),
        sa.Column('status', sa.Enum(
            'pending', 'confirmed', 'rejected', name='suggestedlinkstatus', native_enum=False),
            nullable=False, server_default='pending'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('reviewed_by', sa.String(length=36), nullable=True),
        sa.Column('reviewed_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ['office_id'], ['offices.id'], name=op.f('fk_suggested_official_links_office_id_offices')),
        sa.ForeignKeyConstraint(
            ['candidate_employee_id'], ['employees.id'],
            name=op.f('fk_suggested_official_links_candidate_employee_id_employees')),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_suggested_official_links')),
    )
    op.create_index(
        'ix_suggested_links_office_role', 'suggested_official_links',
        ['office_id', 'role_label'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_suggested_links_office_role', table_name='suggested_official_links')
    op.drop_table('suggested_official_links')

    op.drop_index('ix_community_links_owner', table_name='community_links')
    op.drop_table('community_links')

    op.drop_index(
        'ix_org_settings_office_id', table_name='org_settings',
        sqlite_where=sa.text('office_id IS NOT NULL'),
        mssql_where=sa.text('office_id IS NOT NULL'),
        postgresql_where=sa.text('office_id IS NOT NULL'),
    )
    op.drop_table('org_settings')
