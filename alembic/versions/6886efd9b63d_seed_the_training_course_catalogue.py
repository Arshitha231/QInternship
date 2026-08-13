"""seed the training course catalogue

Revision ID: 6886efd9b63d
Revises: 36c145414911
Create Date: 2026-08-13

A DATA migration, which deserves justifying — schema history is a bad place
for content, and this is the exception rather than a precedent.

What it inserts is reference data: five courses and the five rules for who is
expected to take them. That's the same kind of thing as a country list or a
currency table — it defines the domain, it isn't about any particular person,
and the app is meaningless without it (with no courses defined, every profile
renders "No courses are expected for this role" and the whole feature reads as
broken).

What it deliberately does NOT insert is per-employee course statuses. Those
are ~900 rows about specific people, they'd go stale the moment anyone joins,
and they are synthetic demo data — seed.py and seed_training.py own that, and
burying it in schema history would be genuinely hard to unpick later.
Consequence, stated plainly: after this migration every profile shows its one
or two courses as "Not completed", because a course with no status row is
correctly read as not started. Run seed_training.py to lay the realistic
completed/in-progress/failed mix on top.

The reason this is a migration at all is reach: the deployed database is only
addressable from inside Azure's network boundary (AllowAzureServices is the
sole firewall rule), and the App Service already runs `alembic upgrade head`
at startup. A migration is the one channel that reliably gets data in there
without an interactive session on the container.

Idempotent by inspection rather than by ON CONFLICT, which isn't spelled the
same way on SQLite and T-SQL: if any course already exists, this does nothing
at all, so it can't fight a database that seed.py or seed_training.py has
already populated.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6886efd9b63d'
down_revision: Union[str, Sequence[str], None] = '36c145414911'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Kept in step with seed.py's TRAINING_COURSES / TRAINING_REQUIREMENTS. Scoped
# so every employee lands on exactly one or two courses: SEC-101 is
# company-wide and the rest are keyed to divisions that don't overlap, so
# nothing can stack a third onto anyone.
COURSES = [
    ("SEC-101", "Information Security Awareness",
     "Annual security basics: phishing, device hygiene, incident reporting."),
    ("CONDUCT-102", "Code of Conduct & Ethics",
     "Company conduct standards, conflicts of interest, and how to raise a concern."),
    ("SECDEV-210", "Secure Coding Practices",
     "Threat modelling, input validation, secrets handling, dependency risk."),
    ("FINCTRL-150", "Financial Controls Basics",
     "Segregation of duties, approval thresholds, and audit evidence."),
    ("LEAD-301", "Manager Essentials: Leading a Team",
     "First-line management: feedback, one-to-ones, and performance conversations."),
]

# course code, org unit name (None = company-wide), job title keyword,
# employment type, note
REQUIREMENTS = [
    ("SEC-101", None, None, None, "Everyone, annually."),
    ("SECDEV-210", "Engineering", None, None, "Anyone in the Engineering division."),
    ("FINCTRL-150", "Finance", None, None, "Anyone in the Finance division."),
    ("LEAD-301", "Product", "Manager", None, "Product managers — division AND title must match."),
    ("CONDUCT-102", "Sales & Marketing", None, "fte",
     "Sales & Marketing employees — contractors sign an equivalent through their agency."),
]

courses_table = sa.table(
    "training_courses",
    sa.column("id", sa.Integer),
    sa.column("code", sa.String),
    sa.column("name", sa.String),
    sa.column("description", sa.Text),
    sa.column("is_active", sa.Boolean),
)

requirements_table = sa.table(
    "course_requirements",
    sa.column("course_id", sa.Integer),
    sa.column("org_unit_id", sa.Integer),
    sa.column("job_title_keyword", sa.String),
    sa.column("employment_type", sa.String),
    sa.column("note", sa.String),
)


def upgrade() -> None:
    conn = op.get_bind()

    already_present = conn.execute(sa.text("SELECT COUNT(*) FROM training_courses")).scalar()
    if already_present:
        return  # seed.py / seed_training.py got here first — leave their data alone

    op.bulk_insert(courses_table, [
        {"code": code, "name": name, "description": description, "is_active": True}
        for code, name, description in COURSES
    ])

    course_ids = dict(conn.execute(sa.text("SELECT code, id FROM training_courses")).all())

    # Division names are resolved at run time rather than hardcoded as ids,
    # since org_units is populated by seed.py with whatever ids it happens to
    # generate. On a brand-new database this table is still empty (the
    # migration runs before seeding), so a divisional rule simply can't be
    # placed yet -- it's skipped rather than inserted unscoped, which would
    # silently impose a divisional course on the entire company. seed.py
    # rebuilds all of this properly a moment later anyway.
    org_unit_ids = dict(conn.execute(sa.text("SELECT name, id FROM org_units")).all())

    rows = []
    for code, unit_name, keyword, employment_type, note in REQUIREMENTS:
        if unit_name is not None and unit_name not in org_unit_ids:
            continue
        rows.append({
            "course_id": course_ids[code],
            "org_unit_id": org_unit_ids[unit_name] if unit_name else None,
            "job_title_keyword": keyword,
            "employment_type": employment_type,
            "note": note,
        })
    if rows:
        op.bulk_insert(requirements_table, rows)


def downgrade() -> None:
    """Removes only what upgrade() added, and only by course code.

    Order is forced by the foreign keys: notifications and statuses both
    reference training_courses, so they have to go first. Both are scoped to
    these five courses -- a status row against some other course, or a
    notification about one, is not this migration's to delete.
    """
    conn = op.get_bind()
    codes = tuple(code for code, _, _ in COURSES)
    placeholders = ", ".join(f":c{i}" for i in range(len(codes)))
    params = {f"c{i}": code for i, code in enumerate(codes)}
    subquery = f"SELECT id FROM training_courses WHERE code IN ({placeholders})"

    for table in ("notifications", "employee_course_statuses", "course_requirements"):
        conn.execute(sa.text(f"DELETE FROM {table} WHERE course_id IN ({subquery})"), params)
    conn.execute(sa.text(f"DELETE FROM training_courses WHERE code IN ({placeholders})"), params)
