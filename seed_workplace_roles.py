"""Hire the workplace-facilities and benefits/leave roles the directory lacks.

Fourth script in the same non-destructive family as seed_it_division.py,
seed_training.py and seed_people_data.py, and it exists for the same reason:
seed.py opens by deleting every employee, so the only way it would add these
people on the deployed database is by regenerating all ~500 with new ids,
breaking every bookmarked profile URL and the identity picker with them.

    python seed_workplace_roles.py

WHY THIS EXISTS
---------------
The Community Graph's official-link bootstrapping (app/community_links.py's
generate_suggested_official_links) derives contact suggestions from job
titles, because there is no role column anywhere in this schema. Two of the
roles it looks for matched NOBODY in this directory:

    facilities_admin   badge access, desk issues, mail
    benefits_admin     benefits and leave

That isn't a keyword-guessing problem — a `like '%facilities%'` /
`like '%benefits%'` scan over every active employee returns zero rows,
because the seeded company genuinely has no such staff. Both are among the
things a new hire needs a name for soonest, so the fix is to give the
company the roles rather than to widen the keywords until something
unrelated matches: an IT Support Specialist is not a facilities admin, and
an HR Generalist is not the benefits owner, and pointing new hires at either
would be worse than the empty state it replaced.

WHERE THEY SIT
--------------
A new "Workplace & Benefits" department under the existing People & Culture
division -- deliberately a SIBLING of HR Operations rather than inside it.
generate_suggested_official_links resolves its hr_contact candidates as
"everyone in the HR_ORG_UNIT_NAME subtree" (app/config.py), so filing these
people under HR Operations would also stage all fourteen of them as HR
contacts, burying the actual HR generalists in a review queue twice the size
for no benefit.

One of each role PER OFFICE (forced_office), because both are inherently
location-bound: the person who fixes your badge is the person in your
building, and an office with no candidate gets no suggestion staged for it
at all.

AFTERWARDS
----------
Two follow-ups, in this order:

  1. Rebuild the search index from the SAME place you ran this script --
     `python build_search_index.py`. New employees load fine by id but
     return nothing for a name or org_unit filter until then, and the index
     is shared between local and deployed, so rebuilding from a laptop
     publishes local-only ids into the index the deployed app queries. Same
     warning seed_it_division.py carries at length.
  2. In the app, as HR: Graphs -> Community -> "Generate suggestions", then
     confirm one candidate per office for each role. Nothing here creates a
     community_links edge on its own -- that is the whole point of the
     staging table, and this script does not bypass it.

Against Azure SQL, run it from inside the Azure network boundary (the App
Service's own SSH console) for the same firewall reason as the others.

Idempotent by inspection: if the department already exists it does nothing,
so a second run can't hire everyone twice.
"""
from __future__ import annotations

import sys

from sqlalchemy import select

import seed
from app.db import DATABASE_URL, SessionLocal
from app.models import Employee, Office, OrgUnit, Skill

DIVISION = "People & Culture"
DEPARTMENT = "Workplace & Benefits"

# Titles, not descriptions: these strings are what
# app.community_links._TITLE_KEYWORD_ROLES matches on, so they have to
# contain the keywords ("workplace", "benefits") that map to
# facilities_admin and benefits_admin. tests/test_community_links.py asserts
# that mapping still holds, so renaming a title here without updating the
# keyword set fails a test rather than silently emptying a role.
FACILITIES_TITLE = "Workplace Experience Coordinator"
BENEFITS_TITLE = "Benefits & Leave Specialist"

# Skill pool to draw from. These are People & Culture staff, so HR
# Operations' pool is the right one -- there is no "Workplace" pool and
# inventing one would mean editing seed.py, which this family of scripts
# deliberately only ever reads.
SKILL_DEPT = "HR Operations"


def _reserve_existing_identifiers(session) -> None:
    """Seed the uniqueness sets from what's already in the database.

    seed.make_email/make_slack dedupe against module-level sets that start
    empty in a fresh process. Without this, a new hire called Priya Sharma
    would be handed priya.sharma@example.com -- which already belongs to
    somebody -- and the insert would fail on the unique constraint partway
    through creating a department.
    """
    for email, slack in session.execute(
        select(Employee.work_email, Employee.slack_handle)
    ).all():
        if email:
            seed.used_emails.add(email)
        if slack:
            seed.used_slack.add(slack)


def _unique_name_drawer(session):
    """seed.next_name, wrapped to avoid colliding with existing staff.

    Two separate hazards, both already learned the hard way by
    seed_it_division.py:

    FORCED_NAME_QUEUE is a SEARCH FIXTURE, not a name pool -- two exact
    "Priya Sharma"s so an exact-name lookup is genuinely ambiguous, plus
    near-duplicate pairs for fuzzy matching. It was consumed by the run that
    built this database; a fresh process refills it and would hire them all
    over again, quietly destroying the ambiguity the golden eval tests for.

    And names are drawn from small pools, so even a 14-person hire collides
    with existing staff readily -- a coordinator sharing a name with the VP
    they report to reads as a data bug on the profile page.
    """
    seed.FORCED_NAME_QUEUE.clear()
    taken = {n for (n,) in session.execute(select(Employee.full_name)).all()}
    draw = seed.next_name

    def unique_next_name():
        for _ in range(60):
            first, last = draw()
            if f"{first} {last}" not in taken:
                taken.add(f"{first} {last}")
                return first, last
        # Pools exhausted against a large directory: take the collision
        # rather than loop forever. Duplicate names are legal here, and the
        # directory already contains several on purpose.
        return first, last

    return unique_next_name


def _find_division_lead(session, division: OrgUnit) -> Employee | None:
    """Who the new department's manager reports to: the People & Culture VP,
    falling back to anyone sitting directly in the division. Returns None
    rather than guessing if the division is empty -- a department dangling
    off nothing is worse than a refusal."""
    vp = session.execute(
        select(Employee).where(
            Employee.org_unit_id == division.id,
            Employee.job_title.ilike("VP%"),
            Employee.is_active == True,  # noqa: E712
        )
    ).scalars().first()
    if vp is not None:
        return vp
    return session.execute(
        select(Employee).where(
            Employee.org_unit_id == division.id,
            Employee.is_active == True,  # noqa: E712
        )
    ).scalars().first()


def _assign_skills(session, skills_by_name: dict[str, Skill], hires: list[Employee]) -> int:
    """Skills for the new hires only.

    Deliberately not seed.assign_skills(), which does global engineering on
    top of the per-person draw -- it tops up "Power BI holders in Bangalore"
    to a target count and curates the Terraform mentor pool by looking across
    ALL_EMPLOYEES. Run against a partial population that logic would think
    the company had almost nobody and start handing Power BI to workplace
    coordinators to hit its quota. The per-person part is all this needs.
    """
    from app.models import EmployeeSkill
    from app.models.enums import SkillLevel, SkillSource

    pool = [n for n in seed.DEPT_SKILL_POOL[SKILL_DEPT] if n in skills_by_name]
    added = 0
    for emp in hires:
        if pool:
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
        if "English" in skills_by_name:
            session.add(EmployeeSkill(
                employee_id=emp.id, skill_id=skills_by_name["English"].id,
                level=SkillLevel.working, source=SkillSource.self_reported, verified_at=None,
            ))
            added += 1
    return added


def main() -> int:
    session = SessionLocal()
    try:
        existing = session.execute(
            select(OrgUnit).where(OrgUnit.name == DEPARTMENT)
        ).scalars().first()
        if existing is not None:
            headcount = session.execute(
                select(Employee).where(Employee.org_unit_id == existing.id)
            ).scalars().all()
            print(f"An org unit named {DEPARTMENT!r} already exists (id={existing.id}, "
                  f"{len(headcount)} people directly in it). Nothing to do.")
            return 0

        division = session.execute(
            select(OrgUnit).where(OrgUnit.name == DIVISION)
        ).scalars().first()
        if division is None:
            print(f"No {DIVISION!r} division in this database -- this isn't a seeded "
                  f"directory. Run `python seed.py` first.")
            return 1

        offices = list(session.execute(select(Office)).scalars().all())
        if not offices:
            print("No offices in this database. Both of these roles are per-office, so "
                  "there is nothing to distribute them across.")
            return 1

        lead_reports_to = _find_division_lead(session, division)
        if lead_reports_to is None:
            print(f"Couldn't identify who the {DEPARTMENT} manager should report to "
                  f"(nobody sits directly in {DIVISION!r}). Aborting rather than creating "
                  f"a department that dangles off nothing.")
            return 1

        before = len(session.execute(
            select(Employee).where(Employee.is_active == True)  # noqa: E712
        ).scalars().all())

        skills_by_name = {s.name: s for s in session.execute(select(Skill)).scalars().all()}
        _reserve_existing_identifiers(session)
        seed.next_name = _unique_name_drawer(session)

        department = OrgUnit(name=DEPARTMENT, parent_id=division.id, unit_type="department")
        session.add(department)
        session.flush()

        # The department manager, at the HQ office -- level 3, matching the
        # other department-level managers seed.py creates.
        first, last = seed.next_name()
        manager = seed.make_employee(
            session, first, last, f"{DEPARTMENT} Manager", department, lead_reports_to,
            3, offices, SKILL_DEPT, forced_office=offices[0],
        )
        session.flush()

        # One of each role per office. forced_office rather than
        # make_employee's usual weighted draw: an office that happened not to
        # win the draw would have no candidate, and
        # generate_suggested_official_links stages suggestions per office --
        # so a missed office means its employees never get that contact at
        # all, which is the exact gap this script exists to close.
        per_office: list[tuple[str, str, str]] = []
        for office in offices:
            for title in (FACILITIES_TITLE, BENEFITS_TITLE):
                first, last = seed.next_name()
                emp = seed.make_employee(
                    session, first, last, title, department, manager,
                    5, offices, SKILL_DEPT, forced_office=office,
                )
                per_office.append((office.name, title, emp.full_name))
        session.flush()

        hires = list(seed.ALL_EMPLOYEES)
        skills_added = _assign_skills(session, skills_by_name, hires)

        session.commit()

        print(f"Created the {DEPARTMENT} department under {DIVISION} with {len(hires)} "
              f"new employees.")
        print(f"Database: {DATABASE_URL[:40]}")
        print(f"  reports to        : {lead_reports_to.full_name} ({lead_reports_to.job_title})")
        print(f"  manager           : {manager.full_name}")
        print(f"  offices covered   : {len(offices)} (one of each role in every office)")
        print(f"  skills assigned   : {skills_added}")
        print(f"  existing employees: {before} (untouched)")
        print()
        for office_name, title, full_name in per_office:
            print(f"    {office_name:<18} {title:<32} {full_name}")
        print()
        print("Next: rebuild the search index from this same place "
              "(`python build_search_index.py`), then in the app as HR go to "
              "Graphs -> Community -> Generate suggestions and confirm one candidate "
              "per office for each role.")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
