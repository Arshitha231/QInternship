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

from datetime import date, timedelta

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


def _active_requirement_rows(db: Session) -> list[tuple[CourseRequirement, TrainingCourse]]:
    """Every requirement row against an active course, unfiltered.

    A handful of rows — five in the seeded dataset — so the whole table is
    loaded once and the scoping clauses are applied in Python below. That is
    what makes the bulk resolver possible at all: doing this per employee
    would be one query per person, and the dashboards resolve requirements
    for the entire company at once.
    """
    return list(db.execute(
        select(CourseRequirement, TrainingCourse)
        .join(TrainingCourse, CourseRequirement.course_id == TrainingCourse.id)
        .where(TrainingCourse.is_active == True)  # noqa: E712 — `.is_(True)` renders as `IS 1`, which T-SQL rejects
        .order_by(TrainingCourse.name)
    ).all())


def _scopes_to(requirement: CourseRequirement, employee: Employee, ancestor_ids: list[int]) -> bool:
    """Do this requirement's clauses all admit this employee?

    The clauses are a conjunction and a NULL clause means "don't narrow on
    this", so an all-NULL requirement admits everybody. This is the single
    definition of that rule — both the per-employee and the bulk resolver
    below go through it, so they cannot disagree about who is expected to
    take what.

    job_title_keyword is matched here rather than in SQL for a reason that
    outlived the query: it's a column-against-column LIKE (the pattern lives
    in the requirement row, the haystack in the employee row), which needs
    dialect-specific string concatenation to express.
    """
    if requirement.employment_type is not None and requirement.employment_type != employee.employment_type:
        return False
    if requirement.org_unit_id is not None and requirement.org_unit_id not in ancestor_ids:
        return False
    keyword = (requirement.job_title_keyword or "").strip().lower()
    if keyword and keyword not in (employee.job_title or "").lower():
        return False
    return True


def applicable_requirements_bulk(
    db: Session, employees: list[Employee]
) -> dict[str, list[tuple[CourseRequirement, TrainingCourse]]]:
    """employee id -> the requirement ROWS that scope to them.

    Rows, not courses: two rows can name the same course (a company-wide one
    and a stricter divisional one) and both are returned, because whoever is
    resolving a deadline needs to see all of them to take the earliest.

    The bulk shape exists for the dashboards, which need this for every
    employee in an org unit — or in the whole company — in one go. Cost is
    one query for the requirement table plus one org-unit walk per DISTINCT
    unit, not per employee: 545 people sit in 75 units, and the walk itself
    is primary-key lookups SQLAlchemy's identity map has usually already
    cached.
    """
    rows = _active_requirement_rows(db)
    ancestors: dict[int | None, list[int]] = {}
    out: dict[str, list[tuple[CourseRequirement, TrainingCourse]]] = {}
    for employee in employees:
        unit_id = employee.org_unit_id
        if unit_id not in ancestors:
            ancestors[unit_id] = org_unit_ancestor_ids(db, unit_id)
        ancestor_ids = ancestors[unit_id]
        out[employee.id] = [
            (requirement, course)
            for requirement, course in rows
            if _scopes_to(requirement, employee, ancestor_ids)
        ]
    return out


def applicable_requirements(db: Session, employee: Employee) -> list[tuple[CourseRequirement, TrainingCourse]]:
    """The requirement rows that scope to one employee. See the bulk version
    above, which this delegates to so there is exactly one implementation of
    the scoping rules."""
    return applicable_requirements_bulk(db, [employee])[employee.id]


def required_courses(db: Session, employee: Employee) -> list[TrainingCourse]:
    """Active courses expected of this employee, by name.

    Two requirement rows can name the same course (a company-wide one and a
    stricter divisional one); the course is still expected exactly once.
    """
    out: list[TrainingCourse] = []
    seen: set[int] = set()
    for _requirement, course in applicable_requirements(db, employee):
        if course.id in seen:
            continue
        seen.add(course.id)
        out.append(course)
    return out


def _requirement_due_date(requirement: CourseRequirement, employee: Employee) -> date | None:
    """One requirement row's deadline for one person, or None if it sets no
    deadline at all.

    With both columns set, the LATER wins — due_days_after_hire is a joining
    grace period, not a second deadline to also beat. "Everyone by 30
    September, and new joiners get 60 days from their start date" is one
    rule with a carve-out: it must not make a person who joined six years
    ago retroactively late by their long-past hire+60, which is exactly what
    taking the earlier of the two did. A recent joiner whose grace period
    runs past the fixed date keeps the grace period; everybody else is on
    the fixed date. So the hire-based column can only ever extend a
    deadline, never pull one earlier.

    Across SEPARATE requirement rows naming the same course the opposite
    holds and the earliest wins — see course_due_dates. Two rules, because
    they answer different questions: within a row, which clause applies to
    this person; across rows, which of several independently-filed
    requirements is strictest.

    A due_days_after_hire row against an employee with no hire_date simply
    contributes nothing rather than defaulting to today — an unknown start
    date is not evidence that someone is late.
    """
    candidates: list[date] = []
    if requirement.due_date is not None:
        candidates.append(requirement.due_date)
    if requirement.due_days_after_hire is not None and employee.hire_date is not None:
        candidates.append(employee.hire_date + timedelta(days=requirement.due_days_after_hire))
    return max(candidates) if candidates else None


def _due_dates_from(rows: list[tuple[CourseRequirement, TrainingCourse]],
                    employee: Employee) -> dict[str, date | None]:
    out: dict[str, date | None] = {}
    for requirement, course in rows:
        due = _requirement_due_date(requirement, employee)
        if course.code in out:
            existing = out[course.code]
            # The strictest applicable deadline wins, and a row that sets no
            # deadline never loosens one another row already set.
            if existing is None or (due is not None and due < existing):
                out[course.code] = due
        else:
            out[course.code] = due
    return out


def course_due_dates_bulk(db: Session, employees: list[Employee]) -> dict[str, dict[str, date | None]]:
    """employee id -> {course code: due date or None}. The bulk shape of
    course_due_dates below, for the same reason applicable_requirements_bulk
    exists."""
    per_employee = applicable_requirements_bulk(db, employees)
    return {e.id: _due_dates_from(per_employee[e.id], e) for e in employees}


def course_due_dates(db: Session, employee: Employee) -> dict[str, date | None]:
    """course code -> the date this employee is expected to have finished it
    by, or None where nothing sets a deadline.

    Keyed by `code`, not the internal integer PK, for the same reason the
    provider interface is: everything user-facing in the training half of
    this app speaks in the ids the other team's system knows.

    A course that appears here with a None value is genuinely deadline-free
    and can never be overdue — distinct from a course absent from the dict
    entirely, which was never expected of this person at all. Keeping those
    two apart is the whole point of this module (see the docstring above),
    and the dashboards depend on it: "no deadline recorded" and "doesn't
    apply to them" are different answers and neither is "late".
    """
    return _due_dates_from(applicable_requirements(db, employee), employee)
