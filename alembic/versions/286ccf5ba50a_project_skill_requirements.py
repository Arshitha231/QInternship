"""project skill requirements

Revision ID: 286ccf5ba50a
Revises: 0d944baf8490
Create Date: 2026-08-15

New table only. Records which skills (and minimum level) a project's
delivery actually needs, as set by the project's owner or HR — see
app/project_skills.py. app/continuity.py's delivery-dependency calculation
uses this when present instead of its previous fallback (treating every
Working/Expert skill an assigned employee happens to hold as a candidate
dependency, which overcounts skills the engagement never actually needed).

Not sensitive data, unlike work_authorization_records — no filtered index,
no isolation requirement, just a plain unique constraint so the same
project can't get two rows for the same skill.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '286ccf5ba50a'
down_revision: Union[str, Sequence[str], None] = '0d944baf8490'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('project_skill_requirements',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('project_id', sa.Integer(), nullable=False),
    sa.Column('skill_id', sa.Integer(), nullable=False),
    sa.Column('minimum_level', sa.Enum('learning', 'working', 'expert', name='skilllevel', native_enum=False), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['project_id'], ['projects.id'], name=op.f('fk_project_skill_requirements_project_id_projects')),
    sa.ForeignKeyConstraint(['skill_id'], ['skills.id'], name=op.f('fk_project_skill_requirements_skill_id_skills')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_project_skill_requirements')),
    sa.UniqueConstraint('project_id', 'skill_id', name=op.f('uq_project_skill_requirements_project_id'))
    )


def downgrade() -> None:
    op.drop_table('project_skill_requirements')
