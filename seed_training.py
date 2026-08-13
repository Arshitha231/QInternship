"""Seed ONLY the training-course tables, against an already-populated database.

Why this exists separately from seed.py: seed.py opens with reset_tables(),
which deletes every employee, org unit, project and skill before regenerating
them. That is correct for building a synthetic directory from nothing, and
catastrophic against a database that already has one — running it on the
deployed Azure SQL database would wipe the directory and hand back ~500
different people with different ids, breaking every bookmarked profile URL.

So this script does the narrow thing: it fills in training_courses,
course_requirements, and employee_course_statuses for the employees that are
already there, and touches nothing else. It never deletes an employee, a
project, a skill, or an org unit.

Idempotent: re-running clears only the three training tables (plus
notifications, which reference them) and rebuilds them from the same fixed
RNG seed, so the statuses it produces are stable across runs.

    python seed_training.py

Reads DATABASE_URL like everything else, so it targets whichever database
that points at. Against Azure SQL, run it from somewhere inside the Azure
network boundary — the App Service's own SSH console works, since the only
firewall rule is AllowAzureServices and that's what lets the app reach the
database at all:

    cd /home/site/wwwroot && python seed_training.py
"""
from __future__ import annotations

import sys

from sqlalchemy import delete, func, select

import seed
from app.db import SessionLocal
from app.models import (
    CourseRequirement,
    Employee,
    EmployeeCourseStatus,
    Notification,
    OrgUnit,
    TrainingCourse,
)


def main() -> int:
    session = SessionLocal()
    try:
        employee_count = session.execute(
            select(func.count(Employee.id)).where(Employee.is_active == True)  # noqa: E712
        ).scalar_one()
        if employee_count == 0:
            print("No employees in this database — run `python seed.py` first to build the "
                  "directory, then this script to add training data on top.")
            return 1

        units = {u.name: u for u in session.execute(select(OrgUnit)).scalars().all()}
        if not units:
            print("No org units — this database isn't a seeded directory.")
            return 1

        # Only the training tables. Notifications first: they reference both
        # employees and training_courses, so they have to go before the
        # courses do. Employees, projects, skills and org units are never
        # touched by anything in this file.
        for model in (Notification, EmployeeCourseStatus, CourseRequirement, TrainingCourse):
            session.execute(delete(model))
        session.commit()

        # seed.py's builders iterate its module-level ALL_EMPLOYEES, which is
        # normally filled as it creates people. Point it at the employees
        # this database already has instead.
        seed.ALL_EMPLOYEES.clear()
        seed.ALL_EMPLOYEES.extend(
            session.execute(
                select(Employee).where(Employee.is_active == True)  # noqa: E712
            ).scalars().all()
        )

        courses = seed.build_training_courses(session)
        seed.build_course_requirements(session, courses, units)
        stats = seed.build_course_statuses(session, courses, units)
        session.commit()

        print(f"Seeded {len(courses)} training courses and requirements against "
              f"{len(seed.ALL_EMPLOYEES)} existing employees. No directory data was modified.")
        seed.print_training_verification(stats)
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
