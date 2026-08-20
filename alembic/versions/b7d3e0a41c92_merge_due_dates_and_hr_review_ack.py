"""merge the course due-date and HR-review-acknowledgement migration lines

Both descend from c8f2b6d41a75 and touch different tables — d1a4f7b25c60
adds the two deadline columns to course_requirements, 0e21182a9161 is main's
own mergepoint carrying the work_authorization_records acknowledgement
columns. Nothing to do here beyond joining the lines back into a single
head; same shape as the merge revisions already in this directory.

Revision ID: b7d3e0a41c92
Revises: 0e21182a9161, d1a4f7b25c60
Create Date: 2026-08-19
"""
from typing import Sequence, Union

revision: str = "b7d3e0a41c92"
down_revision: Union[str, Sequence[str], None] = ("0e21182a9161", "d1a4f7b25c60")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
