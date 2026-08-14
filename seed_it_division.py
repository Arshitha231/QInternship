"""Add the IT division to a database that already has a directory.

Third script in the same non-destructive family as seed_training.py and
seed_people_data.py, and it exists for the same reason: seed.py opens by
deleting every employee, so the only way it would create the IT division on
the deployed database is by regenerating all ~500 people with new ids,
breaking every bookmarked profile URL and the identity picker along with them.

This one HIRES rather than backfills — it's the only script here that adds
employees. It creates the IT division, its IT Operations department, the team
units beneath it, and ~30 people to fill them. It never modifies, reparents or
deletes an existing employee or org unit.

    python seed_it_division.py

Idempotent by inspection: if an org unit named "IT" already exists it does
nothing at all, so a second run can't hire the department twice.

Against Azure SQL, run it from inside the Azure network boundary — the App
Service's own SSH console — for the same firewall reason as the others.

AFTERWARDS: rebuild the search index, or the new people are invisible to
search. find_people retrieves through Azure AI Search whenever it's
configured, and the index is built from a snapshot — new employees have
profiles that load fine by id but return nothing for a name or an org_unit
filter until `python build_search_index.py` runs.

Run that rebuild from the SAME place you ran this script. The index name
("employees-index") and the Search resource are shared between local
development and the deployed app, so rebuilding from a laptop publishes
local-only employee ids into the index the deployed app queries — search
results that 404 when opened. Seed the deployed database, then rebuild from
the deployed side.
"""
from __future__ import annotations

import sys

from sqlalchemy import select

import seed
from app.db import DATABASE_URL, SessionLocal
from app.models import Employee, Office, OrgUnit, Skill, TrainingCourse

DIVISION = "IT"
DEPARTMENT = "IT Operations"


def _reserve_existing_identifiers(session) -> None:
    """Seed the uniqueness sets from what's already in the database.

    seed.make_email/make_slack dedupe against module-level sets that start
    empty in a fresh process. Without this, a new hire called Priya Sharma
    would be handed priya.sharma@example.com — which already belongs to
    somebody — and the insert would fail on the unique constraint, halfway
    through creating a department.
    """
    for email, slack in session.execute(
        select(Employee.work_email, Employee.slack_handle)
    ).all():
        if email:
            seed.used_emails.add(email)
        if slack:
            seed.used_slack.add(slack)


def _find_ceo(session) -> Employee | None:
    """The person the new VP reports to.

    Prefers the explicit title, then falls back to an active employee sitting
    at the company root with no manager — some seeded people deliberately have
    a null manager_id, so "no manager" alone isn't specific enough.
    """
    ceo = session.execute(
        select(Employee).where(
            Employee.job_title == "Chief Executive Officer",
            Employee.is_active == True,  # noqa: E712
        )
    ).scalars().first()
    if ceo is not None:
        return ceo

    root = session.execute(
        select(OrgUnit).where(OrgUnit.unit_type == "company")
    ).scalars().first()
    if root is None:
        return None
    return session.execute(
        select(Employee).where(
            Employee.org_unit_id == root.id,
            Employee.manager_id.is_(None),
            Employee.is_active == True,  # noqa: E712
        )
    ).scalars().first()


def _assign_it_skills(session, skills_by_name: dict[str, Skill], hires: list[Employee]) -> int:
    """Skills for the new hires only.

    Deliberately not seed.assign_skills(), which does global engineering on
    top of the per-person draw — it tops up "Power BI holders in Bangalore" to
    a target count and curates the Terraform mentor pool by looking across
    ALL_EMPLOYEES. Run against a partial population that logic would think the
    company had almost nobody and start handing Power BI to IT support staff
    to hit its quota. The per-person part is all this needs.
    """
    from app.models import EmployeeSkill
    from app.models.enums import SkillLevel, SkillSource

    pool = [n for n in seed.DEPT_SKILL_POOL[DEPARTMENT] if n in skills_by_name]
    languages = [n for n in seed.LANGUAGE_SKILLS if n in skills_by_name]
    added = 0
    for emp in hires:
        for name in seed.rng.sample(pool, seed.rng.randint(2, min(4, len(pool)))):
            session.add(EmployeeSkill(
                employee_id=emp.id, skill_id=skills_by_name[name].id,
                level=seed.rng.choices(
                    [SkillLevel.learning, SkillLevel.working, SkillLevel.expert],
                    weights=[25, 55, 20])[0],
                source=seed.rng.choices(
                    [SkillSource.self_reported, SkillSource.inferred, SkillSource.confirmed],
                    weights=[45, 25, 30])[0],
                verified_at=None,
            ))
            added += 1
        if languages:
            session.add(EmployeeSkill(
                employee_id=emp.id, skill_id=skills_by_name["English"].id
                if "English" in skills_by_name else skills_by_name[languages[0]].id,
                level=SkillLevel.working, source=SkillSource.self_reported, verified_at=None,
            ))
            added += 1
    return added


def main() -> int:
    session = SessionLocal()
    try:
        existing = session.execute(
            select(OrgUnit).where(OrgUnit.name == DIVISION)
        ).scalars().first()
        if existing is not None:
            headcount = session.execute(
                select(Employee).where(Employee.org_unit_id == existing.id)
            ).scalars().all()
            print(f"An org unit named {DIVISION!r} already exists (id={existing.id}, "
                  f"{len(headcount)} people directly in it). Nothing to do.")
            return 0

        employees = session.execute(
            select(Employee).where(Employee.is_active == True)  # noqa: E712
        ).scalars().all()
        if not employees:
            print("No employees in this database — run `python seed.py` first to build the "
                  "directory, then this script to add IT on top.")
            return 1

        root = session.execute(
            select(OrgUnit).where(OrgUnit.unit_type == "company")
        ).scalars().first()
        if root is None:
            print("No company-root org unit — this database isn't a seeded directory.")
            return 1

        ceo = _find_ceo(session)
        if ceo is None:
            print("Couldn't identify who the IT VP should report to. Aborting rather than "
                  "creating a division that dangles off nothing.")
            return 1

        offices = list(session.execute(select(Office)).scalars().all())
        skills_by_name = {s.name: s for s in session.execute(select(Skill)).scalars().all()}
        _reserve_existing_identifiers(session)

        # seed.next_name() drains FORCED_NAME_QUEUE before drawing randomly,
        # and that queue is a SEARCH FIXTURE, not a name pool: two exact
        # "Priya Sharma"s so an exact-name lookup is genuinely ambiguous, plus
        # near-duplicate pairs for fuzzy matching. It's already been consumed
        # by the run that built this database. Left alone, a fresh process
        # refills it and hires them all over again — observed: four Priya
        # Sharmas, which quietly destroys the ambiguity the golden eval tests
        # for. IT gets ordinary random names.
        seed.FORCED_NAME_QUEUE.clear()

        # Names are drawn from small pools, so a 30-person hire collides with
        # existing staff readily — the first run produced a VP of IT with the
        # same name as the CEO he reports to, which reads as a data bug on a
        # profile page ("Reports to: Steven Brown" under the heading Steven
        # Brown). Wrapping next_name rather than passing names in, because
        # populate_unit and distribute_ics call it internally; they resolve it
        # through the module namespace, so replacing the attribute is enough.
        taken = {n for (n,) in session.execute(select(Employee.full_name)).all()}
        draw = seed.next_name

        def unique_next_name():
            for _ in range(60):
                first, last = draw()
                if f"{first} {last}" not in taken:
                    taken.add(f"{first} {last}")
                    return first, last
            # Pools exhausted against a very large directory: take the
            # collision rather than loop forever. Duplicate names are legal
            # here and the directory already contains several on purpose.
            return first, last

        seed.next_name = unique_next_name

        # --- org units -----------------------------------------------------
        division = OrgUnit(name=DIVISION, parent_id=root.id, unit_type="division")
        session.add(division)
        session.flush()
        department = OrgUnit(name=DEPARTMENT, parent_id=division.id, unit_type="department")
        session.add(department)
        session.flush()

        # --- leadership ----------------------------------------------------
        first, last = seed.next_name()
        vp = seed.make_employee(session, first, last, f"VP of {DIVISION}", division, ceo,
                                1, offices, DEPARTMENT)
        first, last = seed.next_name()
        director = seed.make_employee(session, first, last, f"Director of {DEPARTMENT}",
                                      department, vp, 2, offices, DEPARTMENT)
        session.flush()

        # --- teams ---------------------------------------------------------
        team_units = []
        for theme, size in seed.DEPT_THEMES[DEPARTMENT].items():
            sizes = seed.split_target(size)
            for name, team_size in zip(seed.theme_team_names(theme, sizes), sizes):
                unit = OrgUnit(name=name, parent_id=department.id, unit_type="team")
                session.add(unit)
                session.flush()
                team_units.append(unit)
                seed.populate_unit(
                    session, offices, DEPARTMENT, unit, team_size, director,
                    manager_level=4, manager_title_fn=lambda u: f"{u.name} Manager",
                )
        session.flush()

        hires = list(seed.ALL_EMPLOYEES)
        skills_added = _assign_it_skills(session, skills_by_name, hires)

        # Training statuses, so the new people don't all show up as the only
        # employees in the company who have never touched a course. seed's
        # builder reads ALL_EMPLOYEES, which in this process holds exactly the
        # new hires — it appends rows and deletes nothing.
        courses = {c.code: c for c in session.execute(select(TrainingCourse)).scalars().all()}
        course_stats = None
        if courses:
            units_by_name = {u.name: u for u in session.execute(select(OrgUnit)).scalars().all()}
            course_stats = seed.build_course_statuses(session, courses, units_by_name)

        session.commit()

        print(f"Created the {DIVISION} division and {DEPARTMENT} department with "
              f"{len(team_units)} teams and {len(hires)} new employees.")
        print(f"Database: {DATABASE_URL[:40]}")
        print(f"  reports to           : {ceo.full_name} ({ceo.job_title})")
        print(f"  VP                   : {vp.full_name}")
        print(f"  Director             : {director.full_name}")
        print(f"  teams                : {', '.join(u.name for u in team_units)}")
        print(f"  skills assigned      : {skills_added}")
        if course_stats:
            print(f"  course status rows   : {sum(course_stats['by_status'].values())}")
        print(f"  existing employees   : {len(employees)} (untouched)")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
