"""Seed the four Staffing Continuity Intelligence demo cases on top of an
already-seeded directory. Non-destructive add-on, same family as
seed_it_division.py/seed_training.py: seed.py deletes every employee on
each run, so this creates real additional data on top rather than baking
anything into the main reseed.

Why this needs new rows at all, not just new WorkAuthorizationRecord rows:
checked the real distribution directly against a seeded directory.db --
the minimum org-wide Working/Expert holder count for any of the 68 seeded
skills is 8, and the minimum headcount for any employee_projects.role
value is far higher. Nothing in a freshly-seeded directory has the
0 / 1-2 / 3+ backup spread the four demo cases need, so this adds two
narrow skills, three client-engagement projects, and a handful of
background staffing rows, using real existing employees throughout (never
fabricated ones), to construct them.

Builds:
  Case A (High)    — sole holder of a new narrow skill, zero org-wide
                     backups, an open-ended assignment the next HR review
                     intersects.
  Case B (Medium)  — a skill held by exactly 2 people org-wide (1 backup
                     once the at-risk employee is excluded).
  Case C (Low)     — an existing, widely-held skill; ample redundancy.
  Case D (HR-only) — a current, verified record whose next_hr_review_date
                     falls AFTER the assignment's end_date: proves the
                     "none" exposure path, and that HR still sees the
                     person via the employee drill-down even though
                     nothing shows up in the engagement-exposure list.

Each case also gets a superseded predecessor record and a genuinely
pending_verification record, so the current/superseded/pending selection
logic (app/continuity._get_current_authorization_record) has real data to
exercise, not just fixture rows.

    python seed_continuity.py

Idempotent by inspection: if a project named after the first new client
engagement already exists, this does nothing at all.
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta

from sqlalchemy import func, select

from app.db import DATABASE_URL, SessionLocal
from app.models import (
    Employee, EmployeeProject, EmployeeSkill, OrgUnit, Project, ProjectSkillRequirement, Skill,
    WorkAuthorizationRecord,
)
from app.models.enums import (
    ProjectClassification, ProjectType, SkillCategory, SkillLevel, SkillSource,
    VerificationStatus, WorkAuthorizationType,
)

MERIDIAN = "Meridian Health — Claims Platform Modernization"
SOLSTICE = "Solstice Retail — POS Integration Programme"
ARCADIA = "Arcadia Logistics — Fleet Analytics Rollout"

FHIR_SKILL = "FHIR Interoperability"
POS_SKILL = "Retail POS Integration"

TODAY = date.today()


def _find_owner(session) -> Employee | None:
    """Any active director-level employee, for the new projects' owner_id.
    Falls back to any active employee -- who owns these doesn't matter for
    the continuity calculation, only that the FK points at a real row."""
    director = session.execute(
        select(Employee).where(Employee.job_title.ilike("%Director%"), Employee.is_active == True)  # noqa: E712
    ).scalars().first()
    if director is not None:
        return director
    return session.execute(
        select(Employee).where(Employee.is_active == True)  # noqa: E712
    ).scalars().first()


def _find_verifier(session) -> Employee | None:
    """An HR-ish employee to attribute record verification to. Purely
    cosmetic -- verified_by isn't read by any continuity calculation --
    so this falls back to None rather than failing if no HR-titled
    employee exists in this dataset."""
    return session.execute(
        select(Employee).where(Employee.job_title.ilike("%HR%"), Employee.is_active == True)  # noqa: E712
    ).scalars().first()


def _pick_unused_employees(session, used_ids: set[str], count: int) -> list[Employee]:
    """Real, active, individual-contributor-ish employees not already
    claimed by an earlier case in this script. Deterministic (ordered by
    id), not random -- a re-run against the same database picks the same
    people."""
    rows = session.execute(
        select(Employee)
        .where(
            Employee.is_active == True,  # noqa: E712
            Employee.job_title.notilike("%Chief%"),
            Employee.job_title.notilike("%VP%"),
            Employee.job_title.notilike("%Director%"),
        )
        .order_by(Employee.id)
    ).scalars().all()
    picked = [e for e in rows if e.id not in used_ids][:count]
    used_ids.update(e.id for e in picked)
    return picked


def _pick_widely_held_skill(session, min_holders: int = 6) -> Skill | None:
    """An existing skill with ample org-wide redundancy, for Case C.
    Queried rather than hardcoded to a specific name so this script stays
    correct even if the underlying seed data changes -- picks whichever
    canonical technical/domain skill has the most Working/Expert holders,
    as long as it clears min_holders. Language skills are excluded, same
    reasoning as app/continuity.py's own dependency computation: a
    delivery dependency is a specialized capability, and general language
    fluency isn't that."""
    rows = session.execute(
        select(Skill, func.count(EmployeeSkill.id).label("n"))
        .join(EmployeeSkill, EmployeeSkill.skill_id == Skill.id)
        .where(
            Skill.canonical_id.is_(None), Skill.category != SkillCategory.language,
            EmployeeSkill.level.in_([SkillLevel.working, SkillLevel.expert]),
        )
        .group_by(Skill.id)
        .order_by(func.count(EmployeeSkill.id).desc())
    ).all()
    for skill, n in rows:
        if n >= min_holders:
            return skill
    return rows[0][0] if rows else None


def _make_project(session, name: str, owner: Employee, unit: OrgUnit) -> Project:
    project = Project(
        name=name, type=ProjectType.project,
        description=f"{name} — client engagement.",
        owning_unit_id=unit.id, owner_id=owner.id,
        classification=ProjectClassification.internal, is_client_engagement=True,
    )
    session.add(project)
    session.flush()
    return project


def _make_record(
    session, employee: Employee, verifier: Employee | None, *,
    authorization_type: WorkAuthorizationType, next_hr_review_date: date | None,
    effective_from: date, effective_until: date | None,
) -> WorkAuthorizationRecord:
    """Current + verified, with one superseded predecessor underneath it
    and one genuinely pending_verification record on top of it -- real
    data for the current/superseded/pending selection logic to exercise.

    The pending row is is_current=False (not True): the DB's filtered
    unique index allows at most one is_current=True row per employee, so
    it can't coexist with the real current+verified row above. The "is
    current=True but not verified" defensive edge case is exercised
    directly in tests/test_continuity.py against a throwaway fixture
    employee instead, where it can be that employee's only record.
    """
    predecessor = WorkAuthorizationRecord(
        employee_id=employee.id, authorization_type=WorkAuthorizationType.cpt,
        effective_from=effective_from - timedelta(days=365), effective_until=effective_from,
        next_hr_review_date=None, verification_status=VerificationStatus.superseded,
        verified_at=datetime.combine(effective_from - timedelta(days=300), datetime.min.time()),
        verified_by=verifier.id if verifier else None, is_current=False,
    )
    session.add(predecessor)
    session.flush()

    current = WorkAuthorizationRecord(
        employee_id=employee.id, authorization_type=authorization_type,
        effective_from=effective_from, effective_until=effective_until,
        next_hr_review_date=next_hr_review_date, verification_status=VerificationStatus.verified,
        verified_at=datetime.combine(effective_from, datetime.min.time()),
        verified_by=verifier.id if verifier else None, is_current=True,
        supersedes_record_id=predecessor.id,
    )
    session.add(current)
    session.flush()

    session.add(WorkAuthorizationRecord(
        employee_id=employee.id, authorization_type=authorization_type,
        effective_from=TODAY - timedelta(days=5), effective_until=effective_until,
        next_hr_review_date=next_hr_review_date, verification_status=VerificationStatus.pending_verification,
        verified_at=None, verified_by=None, is_current=False,
    ))
    return current


def main() -> int:
    session = SessionLocal()
    try:
        existing = session.execute(select(Project).where(Project.name == MERIDIAN)).scalars().first()
        if existing is not None:
            print(f"{MERIDIAN!r} already exists (id={existing.id}). Nothing to do.")
            return 0

        employees_total = session.execute(
            select(func.count()).select_from(Employee).where(Employee.is_active == True)  # noqa: E712
        ).scalar_one()
        if not employees_total:
            print("No employees in this database — run `python seed.py` first.")
            return 1

        owner = _find_owner(session)
        verifier = _find_verifier(session)
        unit = session.execute(select(OrgUnit).where(OrgUnit.unit_type == "department")).scalars().first()
        if owner is None or unit is None:
            print("Couldn't find a plausible project owner/org unit — this doesn't look like a seeded directory.")
            return 1

        used_ids: set[str] = set()

        # --- new narrow skills, for cases A and B --------------------------
        fhir = Skill(name=FHIR_SKILL, category=SkillCategory.technical, canonical_id=None)
        pos = Skill(name=POS_SKILL, category=SkillCategory.technical, canonical_id=None)
        session.add_all([fhir, pos])
        session.flush()

        # --- new client-engagement projects --------------------------------
        meridian = _make_project(session, MERIDIAN, owner, unit)
        solstice = _make_project(session, SOLSTICE, owner, unit)
        arcadia = _make_project(session, ARCADIA, owner, unit)

        # --- declared skill requirements -- what each engagement actually
        # needs, per app/project_skills.py, so these three demo projects
        # exercise the "declared" path (app/continuity.py's
        # _compute_delivery_dependencies) rather than the "inferred"
        # fallback. Without this, Case A/B's dependency lists would also
        # include every OTHER Working/Expert skill the seeded base
        # population happens to have given emp_a/emp_b (Figma, Agile/Scrum,
        # ...) -- correct under the fallback, but noisier than a real
        # engagement with recorded requirements would ever show.
        session.add_all([
            ProjectSkillRequirement(project_id=meridian.id, skill_id=fhir.id, minimum_level=SkillLevel.working),
            ProjectSkillRequirement(project_id=solstice.id, skill_id=pos.id, minimum_level=SkillLevel.working),
        ])

        # --- background staffing: gives Case C's "Contributor" project-role
        # dependency real redundancy. Without this, "Contributor" would be
        # held by nobody else across these 3 brand-new client engagements,
        # which would (wrongly) drag Case C's overall exposure up to High
        # via the role dependency even though the skill dependency is
        # deliberately Low. These people get no work-authorization record
        # of their own -- they're staffing padding, not a 5th demo case.
        background = _pick_unused_employees(session, used_ids, 3)
        for i, emp in enumerate(background):
            session.add(EmployeeProject(
                employee_id=emp.id, project_id=(meridian.id if i % 2 == 0 else solstice.id),
                role="Contributor", start_date=TODAY - timedelta(days=90), end_date=None,
            ))

        # --- Case A: High -- sole FHIR holder, open-ended Meridian assignment
        [emp_a] = _pick_unused_employees(session, used_ids, 1)
        session.add(EmployeeProject(
            employee_id=emp_a.id, project_id=meridian.id, role="Lead",
            start_date=TODAY - timedelta(days=200), end_date=None,
        ))
        session.add(EmployeeSkill(
            employee_id=emp_a.id, skill_id=fhir.id, level=SkillLevel.expert,
            source=SkillSource.confirmed, verified_at=datetime.now(),
        ))
        _make_record(
            session, emp_a, verifier, authorization_type=WorkAuthorizationType.stem_opt,
            next_hr_review_date=TODAY + timedelta(days=53),
            effective_from=TODAY - timedelta(days=300), effective_until=TODAY + timedelta(days=400),
        )

        # --- Case B: Medium -- POS skill held by exactly 2 people org-wide.
        # The 2nd holder is deliberately NOT staffed on Solstice, so they
        # count as an org-wide backup rather than project-level redundancy.
        emp_b, emp_b_backup = _pick_unused_employees(session, used_ids, 2)
        session.add(EmployeeProject(
            employee_id=emp_b.id, project_id=solstice.id, role="Lead",
            start_date=TODAY - timedelta(days=150), end_date=None,
        ))
        session.add_all([
            EmployeeSkill(employee_id=emp_b.id, skill_id=pos.id, level=SkillLevel.expert,
                          source=SkillSource.confirmed, verified_at=datetime.now()),
            EmployeeSkill(employee_id=emp_b_backup.id, skill_id=pos.id, level=SkillLevel.working,
                          source=SkillSource.self_reported, verified_at=None),
        ])
        _make_record(
            session, emp_b, verifier, authorization_type=WorkAuthorizationType.h1b,
            next_hr_review_date=TODAY + timedelta(days=94),
            effective_from=TODAY - timedelta(days=500), effective_until=TODAY + timedelta(days=600),
        )

        # --- Case C: Low -- an existing, widely-held skill; ample redundancy
        [emp_c] = _pick_unused_employees(session, used_ids, 1)
        wide_skill = _pick_widely_held_skill(session)
        session.add(EmployeeProject(
            employee_id=emp_c.id, project_id=arcadia.id, role="Contributor",
            start_date=TODAY - timedelta(days=100), end_date=None,
        ))
        if wide_skill is not None:
            already_has = session.execute(
                select(EmployeeSkill).where(
                    EmployeeSkill.employee_id == emp_c.id, EmployeeSkill.skill_id == wide_skill.id,
                )
            ).scalars().first()
            if already_has is None:
                session.add(EmployeeSkill(
                    employee_id=emp_c.id, skill_id=wide_skill.id, level=SkillLevel.working,
                    source=SkillSource.self_reported, verified_at=None,
                ))
            session.add(ProjectSkillRequirement(
                project_id=arcadia.id, skill_id=wide_skill.id, minimum_level=SkillLevel.working,
            ))
        _make_record(
            session, emp_c, verifier, authorization_type=WorkAuthorizationType.opt,
            next_hr_review_date=TODAY + timedelta(days=121),
            effective_from=TODAY - timedelta(days=200), effective_until=TODAY + timedelta(days=200),
        )

        # --- Case D: HR-only -- review falls AFTER the assignment ends, so
        # it never intersects: proves the "none" exposure path.
        [emp_d] = _pick_unused_employees(session, used_ids, 1)
        assignment_end = TODAY + timedelta(days=60)
        session.add(EmployeeProject(
            employee_id=emp_d.id, project_id=solstice.id, role="Reviewer",
            start_date=TODAY - timedelta(days=250), end_date=assignment_end,
        ))
        _make_record(
            session, emp_d, verifier, authorization_type=WorkAuthorizationType.h1b,
            next_hr_review_date=assignment_end + timedelta(days=90),
            effective_from=TODAY - timedelta(days=400), effective_until=TODAY + timedelta(days=500),
        )

        session.commit()

        print("Seeded the 4 continuity demo cases.")
        print(f"Database: {DATABASE_URL[:60]}")
        print(f"  Case A (High)   : {emp_a.full_name} — {MERIDIAN}")
        print(f"  Case B (Medium) : {emp_b.full_name} — {SOLSTICE} (backup: {emp_b_backup.full_name})")
        print(f"  Case C (Low)    : {emp_c.full_name} — {ARCADIA}"
              f" ({wide_skill.name if wide_skill else 'no widely-held skill found'})")
        print(f"  Case D (none)   : {emp_d.full_name} — {SOLSTICE} (review after assignment ends)")
        print(f"  background staffing: {', '.join(e.full_name for e in background)}")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
