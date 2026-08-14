"""Backfill salary and date of birth onto a database that already has people.

Same reason seed_training.py exists: seed.py opens by deleting every employee,
project, skill and org unit, so it's the wrong tool for a database that
already holds a directory — running it against the deployed one would return
~500 different people with different ids and break every bookmarked profile.

This fills in the two columns migration 12c1545605b0 added, for employees that
don't have them yet, and touches nothing else. It never changes a name, an
id, a reporting line, or an existing salary/date of birth. Safe to re-run:
already-populated rows are skipped, so a second pass is a no-op except for
the date milestones below.

It also plants a few birthdays and milestone anniversaries on today's date,
because the notification sweep is otherwise undemoable — with ~500 people
roughly one birthday falls on any given day and the next 5-year anniversary
might be weeks out, so "run it and see" would usually show an empty result
indistinguishable from a broken sweep.

    python seed_people_data.py

Reads DATABASE_URL like everything else. Against Azure SQL, run it from
inside the Azure network boundary — the App Service's own SSH console — for
the same firewall reason as seed_training.py.
"""
from __future__ import annotations

import sys

from sqlalchemy import select

import seed
from app.db import DATABASE_URL, SessionLocal
from app.models import Employee, Office

# The org level seed.py would have assigned, recovered from the data rather
# than remembered: seed.py fills EMPLOYEE_LEVEL as it builds people, and that
# lives only in the process that ran it. Depth from the CEO reproduces it —
# 0 for whoever has no manager, +1 per hop down — which is exactly how levels
# were assigned in the first place.
MAX_LEVEL = 7


def levels_by_depth(employees: list[Employee]) -> dict[str, int]:
    by_id = {e.id: e for e in employees}
    levels: dict[str, int] = {}

    def depth(emp_id: str, guard: int = 0) -> int:
        if emp_id in levels:
            return levels[emp_id]
        emp = by_id.get(emp_id)
        # Cycle guard, same reasoning as the org chart's: malformed manager_id
        # data must not be able to recurse forever.
        if emp is None or emp.manager_id is None or guard > MAX_LEVEL + 3:
            return 0
        result = min(depth(emp.manager_id, guard + 1) + 1, MAX_LEVEL)
        levels[emp_id] = result
        return result

    for e in employees:
        levels[e.id] = depth(e.id)
    return levels


def main() -> int:
    session = SessionLocal()
    try:
        employees = list(session.execute(
            select(Employee).where(Employee.is_active == True)  # noqa: E712
        ).scalars().all())
        if not employees:
            print("No employees in this database — run `python seed.py` first to build the "
                  "directory, then this script to fill in salary and date of birth.")
            return 1

        offices = {o.id: o for o in session.execute(select(Office)).scalars().all()}
        levels = levels_by_depth(employees)

        filled_dob = 0
        filled_salary = 0
        for emp in employees:
            level = levels.get(emp.id, MAX_LEVEL)
            if emp.date_of_birth is None:
                emp.date_of_birth = seed.make_date_of_birth(level, emp.employment_type, emp.hire_date)
                filled_dob += emp.date_of_birth is not None
            if emp.salary is None:
                office = offices.get(emp.office_id) if emp.office_id else None
                emp.salary, emp.salary_currency = seed.make_salary(level, emp.employment_type, office)
                filled_salary += emp.salary is not None

        # seed.inject_date_milestones reads these two module globals, which are
        # normally filled as seed.py builds people. Point them at what's
        # already in this database instead.
        seed.ALL_EMPLOYEES.clear()
        seed.ALL_EMPLOYEES.extend(employees)
        seed.EMPLOYEE_LEVEL.clear()
        seed.EMPLOYEE_LEVEL.update(levels)
        milestones = seed.inject_date_milestones(session)

        session.commit()

        print(f"Backfilled {filled_dob} dates of birth and {filled_salary} salaries "
              f"across {len(employees)} employees. Names, ids and reporting lines untouched.")
        print(f"Database: {DATABASE_URL[:40]}")
        seed.print_people_data_verification(session, milestones)
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
