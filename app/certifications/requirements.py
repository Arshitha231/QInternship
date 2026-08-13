"""Which courses are expected of whom — the company-side half.

Kept separate from the providers on purpose. A provider answers "what has
this person done", which is the training team's fact; a requirement answers
"what should this person have done", which is ours. Joining them is what
makes an absent status readable: no record against an expected course means
*not started*, and no record against a course that was never expected means
*doesn't apply* — two very different things that look identical without this
module.
"""
from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import CourseRequirement, Employee, OrgUnit, TrainingCourse

# org_units is company -> division -> department -> team, so four hops is the
# real ceiling; the bound is generous and, like every other walk in this
# codebase, exists so malformed parent_id data can't loop forever.
ORG_UNIT_MAX_DEPTH = 10


def org_unit_ancestor_ids(db: Session, org_unit_id: int | None) -> list[int]:
    """The employee's own unit plus every unit above it, nearest first.

    Requirements are filed against a unit and apply to everything under it.
    Walking *up* from the one employee is cheaper than expanding each
    requirement's subtree downward, and needs no recursive CTE — the tree is
    four levels deep and this is a handful of primary-key lookups.
    """
    ids: list[int] = []
    seen: set[int] = set()
    unit = db.get(OrgUnit, org_unit_id) if org_unit_id is not None else None
    hops = 0
    while unit is not None and unit.id not in seen and hops < ORG_UNIT_MAX_DEPTH:
        ids.append(unit.id)
        seen.add(unit.id)
        unit = db.get(OrgUnit, unit.parent_id) if unit.parent_id is not None else None
        hops += 1
    return ids


def required_courses(db: Session, employee: Employee) -> list[TrainingCourse]:
    """Active courses expected of this employee, by name.

    A requirement's clauses are a conjunction, and a NULL clause means "don't
    narrow on this" — so an all-NULL requirement is company-wide. Two
    requirement rows can name the same course (a company-wide one and a
    stricter divisional one); the course is still expected exactly once.
    """
    ancestor_ids = org_unit_ancestor_ids(db, employee.org_unit_id)

    stmt = (
        select(CourseRequirement, TrainingCourse)
        .join(TrainingCourse, CourseRequirement.course_id == TrainingCourse.id)
        .where(
            TrainingCourse.is_active == True,  # noqa: E712 — `.is_(True)` renders as `IS 1`, which T-SQL rejects
            or_(
                CourseRequirement.employment_type.is_(None),
                CourseRequirement.employment_type == employee.employment_type,
            ),
        )
        .order_by(TrainingCourse.name)
    )
    if ancestor_ids:
        stmt = stmt.where(
            or_(CourseRequirement.org_unit_id.is_(None), CourseRequirement.org_unit_id.in_(ancestor_ids))
        )
    else:
        stmt = stmt.where(CourseRequirement.org_unit_id.is_(None))

    # job_title_keyword is matched in Python rather than SQL: it's a
    # column-against-column LIKE (the pattern lives in the requirement row,
    # the haystack in the employee row), which needs dialect-specific string
    # concatenation to express — and this table holds a handful of rows, so
    # the filter costs nothing here and stays portable.
    title = (employee.job_title or "").lower()
    out: list[TrainingCourse] = []
    seen: set[int] = set()
    for requirement, course in db.execute(stmt).all():
        keyword = (requirement.job_title_keyword or "").strip().lower()
        if keyword and keyword not in title:
            continue
        if course.id in seen:
            continue
        seen.add(course.id)
        out.append(course)
    return out
