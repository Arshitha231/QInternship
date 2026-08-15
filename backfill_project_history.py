"""Backfill project history onto employees who have none.

Fourth script in the same non-destructive family as seed_it_division.py,
seed_training.py and seed_people_data.py, for the same reason: seed.py opens
by deleting every employee, so it's the wrong tool for a database that
already holds a directory.

Exists because "everyone should have project history" surfaced 34 active
employees with zero rows in employee_projects -- 29 of them the entire IT
division seed_it_division.py hired. That script assigns skills
(_assign_it_skills) but never called seed.py's project-assignment passes
(build_flagship_projects / build_project_pool / build_portfolios), so IT's
30 people got skills but no projects at all. The other 5 gaps are pre-existing
stragglers from the original seed run.

Touches only employees who currently have zero project rows -- anyone with
even one is left completely alone, so this can never duplicate or dilute an
existing portfolio. Idempotent by construction: a second run finds nobody
left to backfill and does nothing.

For a department with no existing projects to draw from (true for "IT
Operations", which didn't exist when seed.py last ran against this
database), a small starter pool is created first, in the same style/template
set seed.py's build_project_pool uses -- "IT Operations" is already a real
key in seed.DEPT_SKILL_POOL, so the descriptions read as genuinely
IT-specific, not a generic fallback.

    python backfill_project_history.py

Reads DATABASE_URL like everything else. Against Azure SQL, run it from
inside the Azure network boundary -- the App Service's own SSH console --
for the same firewall reason as seed_it_division.py.

AFTERWARDS: rebuild the search index (python build_search_index.py) if
Search is configured -- new project names belong in profile_text, and
nothing here calls the reindex hook itself (see app/search_reindex.py),
matching seed_it_division.py's own precedent of leaving index rebuilds to a
separate, explicit step.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta

from sqlalchemy import select

import seed
from app.db import DATABASE_URL, SessionLocal
from app.models import Employee, EmployeeProject, OrgUnit, Project
from app.models.enums import ProjectClassification, ProjectType

STARTER_POOL_SIZE = 5


def _department_of(session, org_unit_id: int | None) -> OrgUnit | None:
    """Walk parent_id up from an org_unit to its department-level ancestor.
    One level down from app/permissions.py's division_of, same bounded walk
    (company -> division -> department -> team, 4 levels, no risk of
    unbounded recursion).

    Falls back to the STARTING unit itself if no department-level ancestor
    turns up -- the department is a level BELOW division, so walking up from
    someone who sits directly in a division (a VP with no more specific
    department, e.g. seed_it_division.py's "VP of IT" hire) never finds one
    no matter how far it walks. Bucketing that person by their own division
    beats skipping them outright, which is what a bare None here did before
    this fallback existed -- confirmed against this database, which has
    exactly one such person.
    """
    start = session.get(OrgUnit, org_unit_id) if org_unit_id is not None else None
    unit = start
    hops = 0
    while unit is not None and unit.unit_type != "department" and hops < 10:
        unit = session.get(OrgUnit, unit.parent_id) if unit.parent_id is not None else None
        hops += 1
    return unit if unit is not None else start


def _descendant_unit_ids(all_units: list[OrgUnit], root_id: int) -> set[int]:
    """Every org_unit id under root_id, root included. Plain Python BFS over
    an already-fetched list rather than a recursive CTE -- org_units is a
    few hundred rows, small enough that a second query per department would
    be needless, and this avoids the SQLite/SQL Server dialect differences a
    CTE would have to account for (see app/people.py's own precedent for
    walking this same tree)."""
    children: dict[int, list[int]] = {}
    for u in all_units:
        if u.parent_id is not None:
            children.setdefault(u.parent_id, []).append(u.id)
    seen = {root_id}
    frontier = [root_id]
    while frontier:
        nxt = []
        for uid in frontier:
            for child_id in children.get(uid, []):
                if child_id not in seen:
                    seen.add(child_id)
                    nxt.append(child_id)
        frontier = nxt
    return seen


def _pick_department_owner(department_members: list[Employee]) -> Employee | None:
    """Whoever in this department reports to nobody inside it -- the
    department root, same "someone the buck stops with" concept
    seed_it_division.py's _find_ceo uses. Only matters for a department that
    needs a NEW project's owner_id (a real FK, not nullable); departments
    that already have projects never reach this function."""
    if not department_members:
        return None
    member_ids = {m.id for m in department_members}
    roots = [m for m in department_members if m.manager_id is None or m.manager_id not in member_ids]
    return roots[0] if roots else department_members[0]


def _existing_pool(session, department: OrgUnit) -> list[Project]:
    return list(session.execute(
        select(Project).where(
            Project.owning_unit_id == department.id,
            Project.classification != ProjectClassification.confidential,
        )
    ).scalars().all())


def _build_starter_pool(session, department: OrgUnit, owner: Employee) -> list[Project]:
    names = seed.rng.sample(
        seed.COMPLETED_PROJECT_TEMPLATES, min(STARTER_POOL_SIZE, len(seed.COMPLETED_PROJECT_TEMPLATES))
    )
    created: list[Project] = []
    for template in names:
        project = Project(
            name=template.format(dept=department.name),
            type=seed.rng.choices(
                [ProjectType.project, ProjectType.function, ProjectType.system],
                weights=[70, 20, 10])[0],
            description=seed.generic_project_description(template, department.name),
            owning_unit_id=department.id, owner_id=owner.id,
            classification=ProjectClassification.internal,
            is_client_engagement=False,
        )
        session.add(project)
        created.append(project)
    session.flush()
    return created


def _past_dates() -> tuple[date, date]:
    end = date.today() - timedelta(days=seed.rng.randint(10, 700))
    start = end - timedelta(days=seed.rng.randint(60, 400))
    return start, end


def main() -> int:
    session = SessionLocal()
    try:
        employees = list(session.execute(
            select(Employee).where(Employee.is_active == True)  # noqa: E712
        ).scalars().all())
        employees_by_id = {e.id: e for e in employees}

        have_projects = {
            row[0] for row in session.execute(select(EmployeeProject.employee_id).distinct()).all()
        }
        gapped = [e for e in employees if e.id not in have_projects]
        if not gapped:
            print("Every active employee already has at least one project. Nothing to do.")
            return 0

        all_units = list(session.execute(select(OrgUnit)).scalars().all())
        units_by_id = {u.id: u for u in all_units}

        pools: dict[int, list[Project]] = {}
        created_projects = 0
        skipped_no_department = 0
        by_department: dict[str, int] = {}

        for emp in gapped:
            department = _department_of(session, emp.org_unit_id)
            if department is None:
                # No department-level ancestor at all (a root-level or
                # malformed org unit) -- skip rather than guess a home for
                # this person's synthetic project history.
                skipped_no_department += 1
                continue

            if department.id not in pools:
                pool = _existing_pool(session, department)
                if not pool:
                    dept_unit_ids = _descendant_unit_ids(all_units, department.id)
                    dept_members = [e for e in employees if e.org_unit_id in dept_unit_ids]
                    owner = _pick_department_owner(dept_members)
                    if owner is None:
                        skipped_no_department += 1
                        continue
                    pool = _build_starter_pool(session, department, owner)
                    created_projects += len(pool)
                pools[department.id] = pool

            pool = pools[department.id]
            picks = seed.rng.sample(pool, min(len(pool), seed.rng.randint(1, 3)))
            for project in picks:
                ongoing = seed.rng.random() < 0.4
                if ongoing:
                    start = date.today() - timedelta(days=seed.rng.randint(30, 500))
                    end = None
                else:
                    start, end = _past_dates()
                session.add(EmployeeProject(
                    employee_id=emp.id, project_id=project.id,
                    role=seed.rng.choice(seed.PROJECT_ROLES),
                    start_date=start, end_date=end,
                ))
            by_department[department.name] = by_department.get(department.name, 0) + 1

        session.commit()

        backfilled = sum(by_department.values())
        print(f"Backfilled project history for {backfilled} employees.")
        print(f"Created {created_projects} new projects for departments that had none.")
        if skipped_no_department:
            print(f"Skipped {skipped_no_department} employees with no resolvable department.")
        for dept, count in sorted(by_department.items()):
            print(f"  {dept}: {count}")
        print(f"Database: {DATABASE_URL[:40]}")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
