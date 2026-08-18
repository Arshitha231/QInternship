"""Unit tests for app/continuity.py — deterministic, no HTTP, no model call
anywhere (per ARCHITECTURE_2.md §1: "policy is testable without a model
call").

Fixture data is created and torn down per test function, isolated by a
distinctive id/name prefix ("cont-fixture-...", "Continuity Fixture ...")
so it can never collide with or leak into tests/conftest.py's shared
session-scoped seed — mirrors the local-fixture-with-cleanup pattern
already used in tests/test_certifications.py, since continuity's scenarios
(a narrow skill with deliberately zero redundancy, a review landing after
an assignment ends) would be awkward and risky to graft onto the
general-purpose shared seed without some other test's assumptions picking
them up by accident.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError

from app.auth import AuthenticatedUser
from app.continuity import (
    AuthorizationRecordNotActionable,
    AuthorizationRecordNotFound,
    ContinuityForbidden,
    ThresholdConfig,
    _build_engagement_exposure_entry,
    _compute_delivery_dependencies,
    _compute_employee_engagement_entries,
    _compute_engagement_exposure,
    _find_internal_backups,
    _get_current_authorization_record,
    _get_relevant_client_assignments,
    _list_review_queue_items,
    _meets_level,
    _redundancy_source,
    _review_intersects_assignment,
    _severity_for_dependency,
    _severity_for_redundancy,
    confirm_authorization_record,
    get_employee_continuity,
    get_engagement_exposure,
    get_hr_review_queue,
    get_org_exposure,
    reject_authorization_record,
    submit_authorization_record,
)
from app.models import (
    AuditLog, Employee, EmployeeProject, EmployeeSkill, OrgUnit, Office, Project, ProjectSkillRequirement,
    Skill, WorkAuthorizationRecord,
)
from app.models.enums import (
    AvailabilityStatus,
    EmploymentType,
    ProjectClassification,
    ProjectType,
    SkillCategory,
    SkillLevel,
    SkillSource,
    VerificationStatus,
    WorkAuthorizationType,
)

HR = AuthenticatedUser(id="cont-fixture-hr", role="hr", name="Test HR")
EMPLOYEE_CALLER = AuthenticatedUser(id="cont-fixture-employee-caller", role="employee", name="Someone")
MANAGER_CALLER = AuthenticatedUser(id="cont-fixture-manager-caller", role="manager", name="Someone Else")

TODAY = date.today()
CFG = ThresholdConfig(rule_version=1, lookahead_days=90, high_max_redundancy=0, medium_max_redundancy=2)

PREFIX = "cont-fixture-"


def _mkemp(db, key, full_name, org_unit_id, office_id, **overrides):
    fields = dict(
        id=f"{PREFIX}{key}", directory_object_id=None, full_name=full_name, preferred_name=None,
        job_title="Consultant", org_unit_id=org_unit_id, office_id=office_id, manager_id=None,
        work_email=f"{PREFIX}{key}@example.test", work_phone=None, slack_handle=None, timezone=None,
        employment_type=EmploymentType.fte, hire_date=date(2022, 1, 1), cost_centre=None,
        personal_mobile=None, availability_status=AvailabilityStatus.available,
        away_until=None, delegate_id=None, bio=None, photo_url=None, is_active=True,
    )
    fields.update(overrides)
    emp = Employee(**fields)
    db.add(emp)
    return emp


def _mkrecord(db, employee_id, *, is_current, verification_status,
              next_hr_review_date=None, authorization_type=WorkAuthorizationType.h1b,
              supersedes_record_id=None, effective_from=None):
    record = WorkAuthorizationRecord(
        employee_id=employee_id, authorization_type=authorization_type,
        effective_from=effective_from or (TODAY - timedelta(days=200)), effective_until=None,
        next_hr_review_date=next_hr_review_date, verification_status=verification_status,
        verified_at=datetime.now() if verification_status == VerificationStatus.verified else None,
        verified_by=None, is_current=is_current, supersedes_record_id=supersedes_record_id,
    )
    db.add(record)
    db.flush()
    return record


@pytest.fixture
def fx(db_session):
    """Builds the full continuity scenario set once per test, torn down
    afterward. Returns a SimpleNamespace of everything a test might need."""
    db = db_session
    org_unit = db.query(OrgUnit).filter_by(name="Platform Engineering").first()
    office = db.query(Office).first()

    project = Project(
        name="Continuity Fixture Client Engagement", type=ProjectType.project,
        description="fixture", owning_unit_id=org_unit.id, owner_id=None,
        classification=ProjectClassification.internal, is_client_engagement=True,
    )
    # owner_id is NOT NULL on Project -- reuse an existing seeded employee as
    # owner, it plays no role in any continuity calculation.
    any_existing = db.query(Employee).filter(Employee.id.notlike(f"{PREFIX}%")).first()
    project.owner_id = any_existing.id
    db.add(project)
    db.flush()

    other_project = Project(
        name="Continuity Fixture Other Client Engagement", type=ProjectType.project,
        description="fixture", owning_unit_id=org_unit.id, owner_id=any_existing.id,
        classification=ProjectClassification.internal, is_client_engagement=True,
    )
    db.add(other_project)
    db.flush()

    skill_narrow = Skill(name="Continuity Fixture Narrow Skill", category=SkillCategory.technical, canonical_id=None)
    skill_shared = Skill(name="Continuity Fixture Shared Skill", category=SkillCategory.technical, canonical_id=None)
    skill_common = Skill(name="Continuity Fixture Common Skill", category=SkillCategory.technical, canonical_id=None)
    skill_language = Skill(name="Continuity Fixture Language Skill", category=SkillCategory.language, canonical_id=None)
    db.add_all([skill_narrow, skill_shared, skill_common, skill_language])
    db.flush()

    def skill_row(emp_id, skill_id, level=SkillLevel.expert):
        db.add(EmployeeSkill(employee_id=emp_id, skill_id=skill_id, level=level,
                             source=SkillSource.confirmed, verified_at=datetime.now()))

    def assignment(emp_id, project_id, role, end_date):
        db.add(EmployeeProject(employee_id=emp_id, project_id=project_id, role=role,
                               start_date=TODAY - timedelta(days=300), end_date=end_date))

    # --- High: sole holder of skill_narrow, open-ended assignment --------
    high = _mkemp(db, "high", "Fixture High", org_unit.id, office.id)
    db.flush()
    assignment(high.id, project.id, "Lead", None)
    skill_row(high.id, skill_narrow.id)
    _mkrecord(db, high.id, is_current=True, verification_status=VerificationStatus.verified,
             next_hr_review_date=TODAY + timedelta(days=10))

    # --- Medium: skill_shared held by exactly 2 people org-wide. Role is
    # "Stakeholder" -- deliberately not "Contributor" -- so the automatic
    # project_role dependency doesn't collide with fx.project_mate/ended/
    # no_review below (all "Contributor" on the same project, which would
    # give THIS dependency project-level redundancy too and mask the
    # org-wide-only "medium" signal this fixture exists to test). Instead
    # medium_backup holds "Stakeholder" on other_project -- zero project
    # redundancy, exactly one org-wide backup, same "medium" shape as the
    # skill dependency, so the worst-of-both-dependencies result is
    # unambiguously medium either way.
    medium = _mkemp(db, "medium", "Fixture Medium", org_unit.id, office.id)
    medium_backup = _mkemp(db, "medium-backup", "Fixture Medium Backup", org_unit.id, office.id)
    db.flush()
    assignment(medium.id, project.id, "Stakeholder", TODAY + timedelta(days=200))
    skill_row(medium.id, skill_shared.id)
    skill_row(medium_backup.id, skill_shared.id, level=SkillLevel.working)  # not on the project
    db.add(EmployeeProject(employee_id=medium_backup.id, project_id=other_project.id, role="Stakeholder",
                           start_date=TODAY - timedelta(days=300), end_date=None))
    _mkrecord(db, medium.id, is_current=True, verification_status=VerificationStatus.verified,
             next_hr_review_date=TODAY + timedelta(days=30))

    # --- Low: skill_common held by 4 people org-wide (3 backups). The
    # "Reviewer" role dependency ALSO needs >= 3 org-wide backups to land
    # on "low" too (fx.after_review/fx.on_boundary below both hold
    # "Reviewer" on other_project, which is otherwise only 2) -- one of
    # low_backups picks up a third "Reviewer" assignment on other_project
    # purely so this employee's role dependency isn't accidentally
    # "medium" and doesn't mask the skill dependency's "low" signal.
    low = _mkemp(db, "low", "Fixture Low", org_unit.id, office.id)
    low_backups = [_mkemp(db, f"low-backup-{i}", f"Fixture Low Backup {i}", org_unit.id, office.id) for i in range(3)]
    db.flush()
    assignment(low.id, project.id, "Reviewer", TODAY + timedelta(days=300))
    skill_row(low.id, skill_common.id)
    for b in low_backups:
        skill_row(b.id, skill_common.id, level=SkillLevel.working)  # none on the project
    db.add(EmployeeProject(employee_id=low_backups[0].id, project_id=other_project.id, role="Reviewer",
                           start_date=TODAY - timedelta(days=300), end_date=None))
    _mkrecord(db, low.id, is_current=True, verification_status=VerificationStatus.verified,
             next_hr_review_date=TODAY + timedelta(days=30))

    # --- One holder of skill_shared/skill_common IS on the project too,
    # to exercise project_backup_count (vs org_backup_count) distinctly.
    project_mate = _mkemp(db, "project-mate", "Fixture Project Mate", org_unit.id, office.id)
    db.flush()
    assignment(project_mate.id, project.id, "Contributor", None)
    skill_row(project_mate.id, skill_shared.id, level=SkillLevel.working)

    # --- Ended: assignment already over, excluded upstream ---------------
    ended = _mkemp(db, "ended", "Fixture Ended", org_unit.id, office.id)
    db.flush()
    assignment(ended.id, project.id, "Contributor", TODAY - timedelta(days=10))
    _mkrecord(db, ended.id, is_current=True, verification_status=VerificationStatus.verified,
             next_hr_review_date=TODAY + timedelta(days=5))

    # --- No review date on file: must not intersect -----------------------
    no_review = _mkemp(db, "no-review", "Fixture No Review", org_unit.id, office.id)
    db.flush()
    assignment(no_review.id, project.id, "Contributor", None)
    _mkrecord(db, no_review.id, is_current=True, verification_status=VerificationStatus.verified,
             next_hr_review_date=None)

    # --- Review lands AFTER assignment ends: does not intersect (Case D) -
    after_review = _mkemp(db, "after-review", "Fixture After Review", org_unit.id, office.id)
    db.flush()
    assignment(after_review.id, other_project.id, "Reviewer", TODAY + timedelta(days=10))
    _mkrecord(db, after_review.id, is_current=True, verification_status=VerificationStatus.verified,
             next_hr_review_date=TODAY + timedelta(days=50))

    # --- Review lands exactly ON the assignment end date: NOT intersecting
    on_boundary = _mkemp(db, "on-boundary", "Fixture On Boundary", org_unit.id, office.id)
    db.flush()
    boundary_date = TODAY + timedelta(days=40)
    assignment(on_boundary.id, other_project.id, "Reviewer", boundary_date)
    _mkrecord(db, on_boundary.id, is_current=True, verification_status=VerificationStatus.verified,
             next_hr_review_date=boundary_date)

    # --- current/superseded/pending selection -----------------------------
    history_emp = _mkemp(db, "history", "Fixture History", org_unit.id, office.id)
    db.flush()
    old = _mkrecord(db, history_emp.id, is_current=False, verification_status=VerificationStatus.superseded,
                    effective_from=TODAY - timedelta(days=400))
    _mkrecord(db, history_emp.id, is_current=True, verification_status=VerificationStatus.verified,
             next_hr_review_date=TODAY + timedelta(days=15), supersedes_record_id=old.id)

    # Only record is is_current=True but NOT verified -- must not be picked up.
    # (Can't coexist with a separate current+verified row for the same
    # employee -- the DB's filtered unique index forbids two is_current=True
    # rows per employee -- so this employee's ONLY record is this one.)
    pending_only = _mkemp(db, "pending-only", "Fixture Pending Only", org_unit.id, office.id)
    db.flush()
    _mkrecord(db, pending_only.id, is_current=True, verification_status=VerificationStatus.pending_verification,
             next_hr_review_date=TODAY + timedelta(days=5))

    superseded_only = _mkemp(db, "superseded-only", "Fixture Superseded Only", org_unit.id, office.id)
    db.flush()
    _mkrecord(db, superseded_only.id, is_current=False, verification_status=VerificationStatus.superseded)

    no_record = _mkemp(db, "no-record", "Fixture No Record", org_unit.id, office.id)
    db.flush()

    db.commit()

    yield SimpleNamespace(
        project=project, other_project=other_project,
        skill_narrow=skill_narrow, skill_shared=skill_shared, skill_common=skill_common,
        skill_language=skill_language,
        high=high, medium=medium, medium_backup=medium_backup, low=low, low_backups=low_backups,
        project_mate=project_mate, ended=ended, no_review=no_review, after_review=after_review,
        on_boundary=on_boundary, history_emp=history_emp, history_old=old,
        pending_only=pending_only, superseded_only=superseded_only, no_record=no_record,
    )

    # --- teardown: delete everything with the fixture's id/name prefix ----
    db.query(WorkAuthorizationRecord).filter(WorkAuthorizationRecord.employee_id.like(f"{PREFIX}%")).delete(
        synchronize_session=False)
    db.query(ProjectSkillRequirement).filter(
        ProjectSkillRequirement.project_id.in_([project.id, other_project.id])
    ).delete(synchronize_session=False)
    db.query(EmployeeSkill).filter(EmployeeSkill.employee_id.like(f"{PREFIX}%")).delete(synchronize_session=False)
    db.query(EmployeeProject).filter(EmployeeProject.employee_id.like(f"{PREFIX}%")).delete(synchronize_session=False)
    db.query(AuditLog).filter(AuditLog.actor_id.like(f"{PREFIX}%")).delete(synchronize_session=False)
    db.query(Employee).filter(Employee.id.like(f"{PREFIX}%")).delete(synchronize_session=False)
    db.query(Project).filter(Project.name.like("Continuity Fixture%")).delete(synchronize_session=False)
    db.query(Skill).filter(Skill.name.like("Continuity Fixture%")).delete(synchronize_session=False)
    db.commit()


# --- Current-record selection ---------------------------------------------

def test_current_verified_record_is_selected(fx, db_session):
    record = _get_current_authorization_record(db_session, fx.history_emp.id)
    assert record is not None
    assert record.verification_status == VerificationStatus.verified
    assert record.next_hr_review_date == TODAY + timedelta(days=15)


def test_superseded_record_is_ignored(fx, db_session):
    record = _get_current_authorization_record(db_session, fx.superseded_only.id)
    assert record is None


def test_pending_record_is_ignored_even_when_is_current(fx, db_session):
    record = _get_current_authorization_record(db_session, fx.pending_only.id)
    assert record is None


def test_no_record_returns_none(fx, db_session):
    record = _get_current_authorization_record(db_session, fx.no_record.id)
    assert record is None


# --- Engagement intersection -----------------------------------------------

def test_review_before_end_date_intersects():
    assignment = SimpleNamespace(end_date=TODAY + timedelta(days=100))
    assert _review_intersects_assignment(TODAY + timedelta(days=10), assignment) is True


def test_review_after_end_date_does_not_intersect():
    assignment = SimpleNamespace(end_date=TODAY + timedelta(days=10))
    assert _review_intersects_assignment(TODAY + timedelta(days=100), assignment) is False


def test_review_on_end_date_does_not_intersect():
    assignment = SimpleNamespace(end_date=TODAY + timedelta(days=40))
    assert _review_intersects_assignment(TODAY + timedelta(days=40), assignment) is False


def test_open_ended_assignment_always_intersects():
    assignment = SimpleNamespace(end_date=None)
    assert _review_intersects_assignment(TODAY + timedelta(days=99999), assignment) is True


def test_ended_assignment_excluded_from_relevant_assignments(fx, db_session):
    relevant = _get_relevant_client_assignments(db_session, fx.ended.id, TODAY)
    assert relevant == []


def test_open_ended_assignment_is_relevant(fx, db_session):
    relevant = _get_relevant_client_assignments(db_session, fx.high.id, TODAY)
    assert len(relevant) == 1
    assert relevant[0].end_date is None


# --- Dependency counts ------------------------------------------------------

def test_single_holder_skill_has_zero_org_backups(fx, db_session):
    project_backup, org_backup, candidates = _find_internal_backups(
        db_session, "skill", fx.skill_narrow.name, fx.project.id, fx.high.id)
    assert (project_backup, org_backup) == (0, 0)
    assert candidates == []


def test_multi_holder_skill_counts_org_backup_excluding_project_members(fx, db_session):
    # skill_shared: fx.medium (self, excluded), fx.medium_backup (elsewhere ->
    # org backup), fx.project_mate (on the SAME project -> project backup).
    project_backup, org_backup, candidates = _find_internal_backups(
        db_session, "skill", fx.skill_shared.name, fx.project.id, fx.medium.id)
    assert project_backup == 1
    assert org_backup == 1
    assert [c.id for c in candidates] == [fx.medium_backup.id]


def test_low_dependency_has_three_org_backups(fx, db_session):
    _project_backup, org_backup, candidates = _find_internal_backups(
        db_session, "skill", fx.skill_common.name, fx.project.id, fx.low.id)
    assert org_backup == 3
    assert len(candidates) == 3


def test_away_org_backup_is_excluded_from_org_backup_count_and_candidates(fx, db_session):
    # One of fx.low's three org-wide skill_common backups goes away --
    # counting them as redeployable overstates it. org_backup_count and
    # the candidate list must both drop; there's no project_backup_count
    # here to begin with, so it stays 0.
    away_backup = fx.low_backups[0]
    away_backup.availability_status = AvailabilityStatus.away
    db_session.commit()

    project_backup, org_backup, candidates = _find_internal_backups(
        db_session, "skill", fx.skill_common.name, fx.project.id, fx.low.id)
    assert project_backup == 0
    assert org_backup == 2
    assert away_backup.id not in {c.id for c in candidates}


def test_away_project_member_still_counts_toward_project_backup_count(fx, db_session):
    # fx.project_mate is ON fx.project, the same engagement as fx.medium --
    # being away doesn't erase that structural, on-paper redundancy, a
    # different question from org-wide redeployability. Only the org-wide
    # partition filters on availability, not this one.
    fx.project_mate.availability_status = AvailabilityStatus.away
    db_session.commit()

    project_backup, org_backup, _candidates = _find_internal_backups(
        db_session, "skill", fx.skill_shared.name, fx.project.id, fx.medium.id)
    assert project_backup == 1
    assert org_backup == 1


def test_redundancy_source_boundaries():
    # Pure function, no DB needed -- mirrors _severity_for_dependency's own
    # branching exactly, since severity's "low" for both project>0 and
    # org>0-with-project=0 is precisely the distinction this field exists
    # to separate back out.
    assert _redundancy_source(0, 0) == "none"
    assert _redundancy_source(0, 1) == "org"
    assert _redundancy_source(0, 3) == "org"
    assert _redundancy_source(1, 0) == "project"
    assert _redundancy_source(1, 3) == "project"  # project coverage wins regardless of org count


def test_redundancy_source_on_real_dependencies_matches_the_fixture_shapes(fx, db_session):
    # Same three fixtures test_review_queue_includes_every_intersecting_severity
    # already pins at the severity level -- confirms the new field lines up
    # with what those "high"/"medium"/"low" results actually mean underneath,
    # and that severity itself is unchanged by this field's addition.
    high_assignment = _get_relevant_client_assignments(db_session, fx.high.id, TODAY)[0]
    high_deps, _ = _compute_delivery_dependencies(db_session, fx.high, fx.project, high_assignment)
    narrow_dep = next(d for d in high_deps if d.name == fx.skill_narrow.name)
    assert narrow_dep.redundancy_source == "none"  # (0, 0) -- the genuine single-point-of-failure case

    medium_assignment = _get_relevant_client_assignments(db_session, fx.medium.id, TODAY)[0]
    medium_deps, _ = _compute_delivery_dependencies(db_session, fx.medium, fx.project, medium_assignment)
    shared_dep = next(d for d in medium_deps if d.name == fx.skill_shared.name)
    assert shared_dep.redundancy_source == "project"  # fx.project_mate is on the same engagement

    low_assignment = _get_relevant_client_assignments(db_session, fx.low.id, TODAY)[0]
    low_deps, _ = _compute_delivery_dependencies(db_session, fx.low, fx.project, low_assignment)
    common_dep = next(d for d in low_deps if d.name == fx.skill_common.name)
    assert common_dep.redundancy_source == "org"  # 0 on-project, 3 org-wide -- the case Option B distinguishes

    # And severity itself is untouched -- "project" and "org" both still
    # land on "low", exactly as before this field existed.
    for dep in (shared_dep, common_dep):
        assert _severity_for_dependency(dep, CFG) == "low"
    assert _severity_for_dependency(narrow_dep, CFG) == "high"


def test_backup_candidates_never_say_replace(fx, db_session):
    _pb, _ob, candidates = _find_internal_backups(
        db_session, "skill", fx.skill_shared.name, fx.project.id, fx.medium.id)
    for c in candidates:
        assert "replace" not in c.matching_evidence.lower()


def test_language_skill_is_never_a_dependency(fx, db_session):
    """A language skill is fluency, not a specialized delivery capability
    an engagement could be short-staffed on -- app/continuity.py excludes
    SkillCategory.language from _compute_delivery_dependencies."""
    from app.models.enums import SkillLevel as _SL
    db_session.add(EmployeeSkill(employee_id=fx.high.id, skill_id=fx.skill_language.id,
                                 level=_SL.expert, source=SkillSource.confirmed, verified_at=datetime.now()))
    db_session.commit()
    record = _get_current_authorization_record(db_session, fx.high.id)
    assignment = _get_relevant_client_assignments(db_session, fx.high.id, TODAY)[0]
    dependencies, _backups = _compute_delivery_dependencies(
        db_session, db_session.get(Employee, fx.high.id), fx.project, assignment)
    assert fx.skill_language.name not in {d.name for d in dependencies}


# --- Declared vs inferred skill dependencies (app/project_skills.py) -------

@pytest.mark.parametrize("level,minimum,expected", [
    (SkillLevel.learning, SkillLevel.learning, True),
    (SkillLevel.working, SkillLevel.learning, True),
    (SkillLevel.expert, SkillLevel.learning, True),
    (SkillLevel.learning, SkillLevel.working, False),
    (SkillLevel.working, SkillLevel.working, True),
    (SkillLevel.expert, SkillLevel.working, True),
    (SkillLevel.working, SkillLevel.expert, False),
    (SkillLevel.expert, SkillLevel.expert, True),
])
def test_meets_level(level, minimum, expected):
    assert _meets_level(level, minimum) is expected


def test_no_project_skill_requirements_falls_back_to_inferred(fx, db_session):
    """No ProjectSkillRequirement rows recorded for fx.project at all --
    the default state -- so every Working/Expert skill fx.high holds is
    still a candidate dependency, tagged "inferred"."""
    assignment = _get_relevant_client_assignments(db_session, fx.high.id, TODAY)[0]
    dependencies, _backups = _compute_delivery_dependencies(
        db_session, db_session.get(Employee, fx.high.id), fx.project, assignment)
    skill_deps = [d for d in dependencies if d.type == "skill"]
    assert {d.name for d in skill_deps} == {fx.skill_narrow.name}
    assert all(d.source == "inferred" for d in skill_deps)


def test_declared_requirement_narrows_to_only_required_skills(fx, db_session):
    """Once ANY requirement is recorded for a project, the inferred
    fallback stops applying entirely for that project -- only skills the
    project actually declared as needed (and that this employee meets)
    show up, source="declared". A second, unrelated skill the employee
    happens to hold is excluded even though it would have counted under
    the old inferred-only behavior."""
    unrelated = Skill(name="Continuity Fixture Unrelated Skill", category=SkillCategory.technical, canonical_id=None)
    db_session.add(unrelated)
    db_session.flush()
    db_session.add(EmployeeSkill(employee_id=fx.high.id, skill_id=unrelated.id, level=SkillLevel.expert,
                                 source=SkillSource.confirmed, verified_at=datetime.now()))
    db_session.add(ProjectSkillRequirement(
        project_id=fx.project.id, skill_id=fx.skill_narrow.id, minimum_level=SkillLevel.working))
    db_session.commit()

    assignment = _get_relevant_client_assignments(db_session, fx.high.id, TODAY)[0]
    dependencies, _backups = _compute_delivery_dependencies(
        db_session, db_session.get(Employee, fx.high.id), fx.project, assignment)
    skill_deps = [d for d in dependencies if d.type == "skill"]

    assert {d.name for d in skill_deps} == {fx.skill_narrow.name}  # NOT "Continuity Fixture Unrelated Skill"
    assert all(d.source == "declared" for d in skill_deps)


def test_declared_requirement_excludes_holder_below_minimum_level(fx, db_session):
    """fx.high holds skill_narrow at Expert (see the fixture), but the
    project declares it needs at least Expert too -- still counts. Declare
    a SECOND, harder requirement (a skill fx.high holds only at Working,
    required at Expert) and confirm that one is silently absent rather
    than appearing as a dependency they don't actually satisfy."""
    hard_skill = Skill(name="Continuity Fixture Hard Requirement", category=SkillCategory.technical, canonical_id=None)
    db_session.add(hard_skill)
    db_session.flush()
    db_session.add(EmployeeSkill(employee_id=fx.high.id, skill_id=hard_skill.id, level=SkillLevel.working,
                                 source=SkillSource.confirmed, verified_at=datetime.now()))
    db_session.add(ProjectSkillRequirement(
        project_id=fx.project.id, skill_id=fx.skill_narrow.id, minimum_level=SkillLevel.working))
    db_session.add(ProjectSkillRequirement(
        project_id=fx.project.id, skill_id=hard_skill.id, minimum_level=SkillLevel.expert))
    db_session.commit()

    assignment = _get_relevant_client_assignments(db_session, fx.high.id, TODAY)[0]
    dependencies, _backups = _compute_delivery_dependencies(
        db_session, db_session.get(Employee, fx.high.id), fx.project, assignment)
    skill_deps = [d for d in dependencies if d.type == "skill"]

    assert {d.name for d in skill_deps} == {fx.skill_narrow.name}
    assert hard_skill.name not in {d.name for d in skill_deps}


def test_project_role_dependency_source_is_always_declared(fx, db_session):
    """employee_projects.role is a real recorded fact regardless of
    whether a project has any ProjectSkillRequirement rows -- never
    "inferred", with or without declared skill requirements."""
    assignment = _get_relevant_client_assignments(db_session, fx.high.id, TODAY)[0]
    dependencies, _backups = _compute_delivery_dependencies(
        db_session, db_session.get(Employee, fx.high.id), fx.project, assignment)
    role_dep = next(d for d in dependencies if d.type == "project_role")
    assert role_dep.source == "declared"


# --- Severity floor semantics -----------------------------------------------

@pytest.mark.parametrize("redundancy,expected", [
    (0, "high"), (1, "medium"), (2, "medium"), (3, "low"), (50, "low"),
])
def test_severity_floor_semantics(redundancy, expected):
    assert _severity_for_redundancy(redundancy, CFG) == expected


# --- Planning window arithmetic ---------------------------------------------

def test_planning_window_arithmetic(fx, db_session):
    record = _get_current_authorization_record(db_session, fx.high.id)
    assignment = _get_relevant_client_assignments(db_session, fx.high.id, TODAY)[0]
    employee = db_session.get(Employee, fx.high.id)
    entry = _build_engagement_exposure_entry(db_session, employee, fx.project, assignment, record, CFG, TODAY)
    assert entry.days_until_hr_review == 10
    assert entry.days_of_assignment_remaining_after_review is None  # open-ended


def test_planning_window_with_a_fixed_end_date(fx, db_session):
    record = _get_current_authorization_record(db_session, fx.medium.id)
    assignment = _get_relevant_client_assignments(db_session, fx.medium.id, TODAY)[0]
    employee = db_session.get(Employee, fx.medium.id)
    entry = _build_engagement_exposure_entry(db_session, employee, fx.project, assignment, record, CFG, TODAY)
    assert entry.days_until_hr_review == 30
    assert entry.days_of_assignment_remaining_after_review == 170  # 200 - 30


# --- Entry-level exposure: none / no-review / on-boundary -------------------

def test_no_review_date_on_file_means_no_exposure(fx, db_session):
    record = _get_current_authorization_record(db_session, fx.no_review.id)
    assignment = _get_relevant_client_assignments(db_session, fx.no_review.id, TODAY)[0]
    employee = db_session.get(Employee, fx.no_review.id)
    entry = _build_engagement_exposure_entry(db_session, employee, fx.project, assignment, record, CFG, TODAY)
    assert entry.exposure == "none"
    assert entry.dependencies == []


def test_review_after_assignment_end_means_no_exposure(fx, db_session):
    record = _get_current_authorization_record(db_session, fx.after_review.id)
    assignment = _get_relevant_client_assignments(db_session, fx.after_review.id, TODAY)[0]
    employee = db_session.get(Employee, fx.after_review.id)
    entry = _build_engagement_exposure_entry(db_session, employee, fx.other_project, assignment, record, CFG, TODAY)
    assert entry.exposure == "none"


# --- Reasons are factual, never speculative about the employee -------------

def test_reasons_never_speculate_about_the_employee(fx, db_session):
    record = _get_current_authorization_record(db_session, fx.high.id)
    assignment = _get_relevant_client_assignments(db_session, fx.high.id, TODAY)[0]
    employee = db_session.get(Employee, fx.high.id)
    entry = _build_engagement_exposure_entry(db_session, employee, fx.project, assignment, record, CFG, TODAY)
    banned = ["risk", "visa", "authorization status", "may lose", "likely"]
    for reason in entry.reasons:
        lowered = reason.lower()
        for word in banned:
            assert word not in lowered, f"reason leaked speculative language: {reason!r}"


# --- End-to-end: org-wide aggregation picks the worst severity -------------

def test_org_wide_exposure_picks_worst_severity_across_dependents(fx, db_session):
    entry = _compute_engagement_exposure(db_session, fx.project, CFG, TODAY)
    assert entry is not None
    # high (fx.high) + medium (fx.medium) + low (fx.low) all intersect ->
    # overall severity is the worst of the three.
    assert entry.exposure == "high"
    assert entry.intersecting_review_count == 3  # high, medium, low -- ended/no_review excluded


def test_employee_drill_down_shows_every_assignment_including_none(fx, db_session):
    entries = _compute_employee_engagement_entries(
        db_session, db_session.get(Employee, fx.after_review.id),
        _get_current_authorization_record(db_session, fx.after_review.id), CFG, TODAY,
    )
    assert len(entries) == 1
    assert entries[0].exposure == "none"


# --- Public functions: role gate + audit ------------------------------------

def test_get_org_exposure_forbidden_for_employee(fx, db_session):
    with pytest.raises(ContinuityForbidden):
        get_org_exposure(db_session, EMPLOYEE_CALLER)


def test_get_org_exposure_forbidden_for_manager(fx, db_session):
    with pytest.raises(ContinuityForbidden):
        get_org_exposure(db_session, MANAGER_CALLER)


def test_get_engagement_exposure_forbidden_for_employee(fx, db_session):
    with pytest.raises(ContinuityForbidden):
        get_engagement_exposure(db_session, EMPLOYEE_CALLER)


def test_get_employee_continuity_forbidden_for_employee(fx, db_session):
    with pytest.raises(ContinuityForbidden):
        get_employee_continuity(db_session, EMPLOYEE_CALLER, fx.high.id)


def test_get_org_exposure_hr_succeeds_and_writes_one_audit_row(fx, db_session):
    before = db_session.query(AuditLog).filter_by(actor_id=HR.id).count()
    overview = get_org_exposure(db_session, HR, window_days=365)
    after = db_session.query(AuditLog).filter_by(actor_id=HR.id).count()
    assert after == before + 1
    assert overview.by_severity["high"] >= 1

    row = db_session.query(AuditLog).filter_by(actor_id=HR.id).order_by(AuditLog.id.desc()).first()
    assert row.action == "continuity.overview_view"
    # Never a raw authorization type or verification word in the audit trail.
    for leaky in ("h1b", "opt", "stem_opt", "verified", "pending"):
        assert leaky not in row.query_text.lower()


def test_get_employee_continuity_returns_full_history(fx, db_session):
    detail = get_employee_continuity(db_session, HR, fx.history_emp.id)
    assert detail is not None
    assert detail.current_record.verification_status == "verified"
    assert len(detail.history) == 2
    statuses = {h.verification_status for h in detail.history}
    assert statuses == {"verified", "superseded"}


def test_get_employee_continuity_unknown_employee_returns_none(db_session):
    assert get_employee_continuity(db_session, HR, "does-not-exist") is None


# --- HR Review Queue: the proactive "who is nearing a review date" list,
# independent of engagement intersection -----------------------------------

def test_review_queue_includes_every_intersecting_severity(fx, db_session):
    items = _list_review_queue_items(db_session, CFG, TODAY, window_days=365)
    by_name = {i.employee.full_name: i for i in items}
    assert by_name["Fixture High"].highest_exposure == "high"
    assert by_name["Fixture High"].engagements_affected == 1
    assert by_name["Fixture Medium"].highest_exposure == "medium"
    assert by_name["Fixture Low"].highest_exposure == "low"


def test_review_queue_includes_non_intersecting_reviews_too(fx, db_session):
    # fx.after_review's review lands after their assignment ends -- zero
    # engagement consequence, but HR still needs to track the review
    # itself. This is exactly the case the org-wide engagement list
    # (_list_engagement_exposures) can never surface.
    items = _list_review_queue_items(db_session, CFG, TODAY, window_days=365)
    by_name = {i.employee.full_name: i for i in items}
    assert "Fixture After Review" in by_name
    assert by_name["Fixture After Review"].engagements_affected == 0
    assert by_name["Fixture After Review"].highest_exposure == "none"


def test_review_queue_excludes_employees_with_no_review_date(fx, db_session):
    items = _list_review_queue_items(db_session, CFG, TODAY, window_days=365)
    names = {i.employee.full_name for i in items}
    assert "Fixture No Review" not in names


def test_review_queue_excludes_unverified_and_recordless_employees(fx, db_session):
    items = _list_review_queue_items(db_session, CFG, TODAY, window_days=365)
    names = {i.employee.full_name for i in items}
    assert "Fixture Pending Only" not in names
    assert "Fixture Superseded Only" not in names
    assert "Fixture No Record" not in names


def test_review_queue_respects_window_days(fx, db_session):
    # fx.high's review is 10 days out, fx.low's is 30 -- a 15-day window
    # keeps the first and drops the second.
    items = _list_review_queue_items(db_session, CFG, TODAY, window_days=15)
    names = {i.employee.full_name for i in items}
    assert "Fixture High" in names
    assert "Fixture Low" not in names


def test_review_queue_filters_by_authorization_type(fx, db_session):
    # Every fx record defaults to h1b (_mkrecord's own default) -- filtering
    # for it keeps everyone, filtering for a type nobody holds returns none.
    kept = _list_review_queue_items(db_session, CFG, TODAY, window_days=365, authorization_type="h1b")
    assert "Fixture High" in {i.employee.full_name for i in kept}
    dropped = _list_review_queue_items(db_session, CFG, TODAY, window_days=365, authorization_type="opt")
    assert "Fixture High" not in {i.employee.full_name for i in dropped}


def test_review_queue_filters_by_exposure(fx, db_session):
    high_only = _list_review_queue_items(db_session, CFG, TODAY, window_days=365, exposure="high")
    names = {i.employee.full_name for i in high_only}
    assert names == {"Fixture High"}

    # "none" is a real, meaningful value here (unlike the engagements-only
    # list) -- fx.after_review's review doesn't intersect anything.
    none_only = _list_review_queue_items(db_session, CFG, TODAY, window_days=365, exposure="none")
    none_names = {i.employee.full_name for i in none_only}
    assert "Fixture After Review" in none_names
    assert "Fixture High" not in none_names


def test_review_queue_filters_by_next_review_date_range(fx, db_session):
    # fx.high is 10 days out, fx.medium/low are 30, fx.after_review is 50 --
    # a 0-15 day window keeps only the first.
    items = _list_review_queue_items(
        db_session, CFG, TODAY, window_days=365,
        next_review_from=TODAY, next_review_to=TODAY + timedelta(days=15),
    )
    names = {i.employee.full_name for i in items}
    assert "Fixture High" in names
    assert "Fixture Medium" not in names
    assert "Fixture Low" not in names
    assert "Fixture After Review" not in names


def test_review_queue_engagements_filter_defaults_keep_zero_engagement_employees(fx, db_session):
    # The point of this endpoint: a review with zero delivery consequence
    # must stay visible unless a caller actively narrows it out. Leaving
    # engagements_min/engagements_max unset (their default) must never do
    # that narrowing by accident.
    items = _list_review_queue_items(db_session, CFG, TODAY, window_days=365)
    assert "Fixture After Review" in {i.employee.full_name for i in items}


def test_review_queue_engagements_min_excludes_zero_when_explicitly_set(fx, db_session):
    items = _list_review_queue_items(db_session, CFG, TODAY, window_days=365, engagements_min=1)
    names = {i.employee.full_name for i in items}
    assert "Fixture After Review" not in names
    assert "Fixture High" in names


def test_review_queue_engagements_max_isolates_zero_engagement_employees(fx, db_session):
    items = _list_review_queue_items(db_session, CFG, TODAY, window_days=365, engagements_max=0)
    names = {i.employee.full_name for i in items}
    assert "Fixture After Review" in names
    assert "Fixture High" not in names
    assert "Fixture Medium" not in names
    assert "Fixture Low" not in names


def test_review_queue_sorted_by_days_until_review_ascending():
    # Pure ordering check against already-built items, no DB needed.
    from app.schemas import AuthorizationRecordOut, HrReviewQueueItem, PersonRef

    def item(name, days):
        return HrReviewQueueItem(
            employee=PersonRef(id=name, full_name=name),
            current_record=AuthorizationRecordOut(
                id=1, authorization_type="h1b", effective_from=TODAY, effective_until=None,
                next_hr_review_date=TODAY + timedelta(days=days), verification_status="verified",
                is_current=True, verified_at=None,
            ),
            days_until_hr_review=days, engagements_affected=0, highest_exposure="none",
        )

    unsorted = [item("c", 30), item("a", 5), item("b", 15)]
    unsorted.sort(key=lambda i: i.days_until_hr_review)
    assert [i.employee.id for i in unsorted] == ["a", "b", "c"]


def test_get_hr_review_queue_forbidden_for_employee(fx, db_session):
    with pytest.raises(ContinuityForbidden):
        get_hr_review_queue(db_session, EMPLOYEE_CALLER)


def test_get_hr_review_queue_forbidden_for_manager(fx, db_session):
    with pytest.raises(ContinuityForbidden):
        get_hr_review_queue(db_session, MANAGER_CALLER)


def test_get_hr_review_queue_hr_succeeds_and_writes_one_audit_row(fx, db_session):
    before = db_session.query(AuditLog).filter_by(actor_id=HR.id).count()
    items = get_hr_review_queue(db_session, HR, window_days=365)
    after = db_session.query(AuditLog).filter_by(actor_id=HR.id).count()
    assert after == before + 1
    assert len(items) >= 4  # high, medium, low, after-review at minimum

    row = db_session.query(AuditLog).filter_by(actor_id=HR.id).order_by(AuditLog.id.desc()).first()
    assert row.action == "continuity.review_queue_view"
    for leaky in ("h1b", "opt", "stem_opt", "verified", "pending"):
        assert leaky not in row.query_text.lower()


def test_get_hr_review_queue_audit_never_leaks_authorization_type_or_dates_even_when_filtered(fx, db_session):
    # Same discipline _write_audit's own docstring states -- target is never
    # a raw authorization type or date -- but now actually exercised with
    # those filters SET, not just left at their default None.
    get_hr_review_queue(
        db_session, HR, window_days=365, authorization_type="h1b",
        next_review_from=TODAY, next_review_to=TODAY + timedelta(days=100),
    )
    row = db_session.query(AuditLog).filter_by(actor_id=HR.id).order_by(AuditLog.id.desc()).first()
    for leaky in ("h1b", "opt", "stem_opt", str(TODAY), str(TODAY + timedelta(days=100))):
        assert leaky not in row.query_text.lower()
    # The fact that a filter WAS applied is still recorded -- just not its value.
    assert "authorization_type_filtered=true" in row.query_text.lower()
    assert "next_review_range_filtered=true" in row.query_text.lower()


# --- Write path: submit / confirm / reject ----------------------------------
# fx.high already carries a current, verified record from the fixture --
# exactly the scenario the verification gate exists for: submitting a
# replacement must not disturb it until someone confirms.

def test_submit_creates_inert_pending_row(fx, db_session):
    out = submit_authorization_record(
        db_session, HR, fx.high.id, authorization_type="opt",
        effective_from=TODAY, next_hr_review_date=TODAY + timedelta(days=400),
    )
    assert out.verification_status == "pending_verification"
    assert out.is_current is False

    row = db_session.get(WorkAuthorizationRecord, out.id)
    assert row.supersedes_record_id is None  # computed at confirm time, not submit time


def test_submit_does_not_disturb_the_existing_current_record(fx, db_session):
    before = _get_current_authorization_record(db_session, fx.high.id)
    submit_authorization_record(
        db_session, HR, fx.high.id, authorization_type="opt", effective_from=TODAY,
    )
    after = _get_current_authorization_record(db_session, fx.high.id)
    assert after is not None
    assert after.id == before.id
    assert after.is_current is True
    assert after.verification_status == VerificationStatus.verified


def test_submit_unknown_employee_raises_not_found(fx, db_session):
    with pytest.raises(AuthorizationRecordNotFound):
        submit_authorization_record(
            db_session, HR, "cont-fixture-does-not-exist", authorization_type="opt", effective_from=TODAY,
        )


@pytest.mark.parametrize("caller", [EMPLOYEE_CALLER, MANAGER_CALLER])
def test_submit_forbidden_for_non_hr(fx, db_session, caller):
    with pytest.raises(ContinuityForbidden):
        submit_authorization_record(db_session, caller, fx.high.id, authorization_type="opt", effective_from=TODAY)


def test_submit_writes_audit_row_without_leaking_authorization_type(fx, db_session):
    before = db_session.query(AuditLog).filter_by(actor_id=HR.id).count()
    submit_authorization_record(db_session, HR, fx.high.id, authorization_type="h1b", effective_from=TODAY)
    after = db_session.query(AuditLog).filter_by(actor_id=HR.id).count()
    assert after == before + 1

    row = db_session.query(AuditLog).filter_by(actor_id=HR.id).order_by(AuditLog.id.desc()).first()
    assert row.action == "continuity.authorization_submitted"
    assert row.query_text == fx.high.id
    assert "h1b" not in row.query_text.lower()


def test_confirm_supersedes_the_prior_current_record(fx, db_session):
    previous = _get_current_authorization_record(db_session, fx.high.id)
    pending = submit_authorization_record(
        db_session, HR, fx.high.id, authorization_type="stem_opt", effective_from=TODAY,
    )

    confirmed = confirm_authorization_record(db_session, HR, pending.id)

    assert confirmed.is_current is True
    assert confirmed.verification_status == "verified"
    confirmed_row = db_session.get(WorkAuthorizationRecord, confirmed.id)
    assert confirmed_row.supersedes_record_id == previous.id
    assert confirmed_row.verified_by == HR.id
    assert confirmed_row.verified_at is not None

    old_row = db_session.get(WorkAuthorizationRecord, previous.id)
    assert old_row.is_current is False
    assert old_row.verification_status == VerificationStatus.superseded


def test_confirm_read_path_now_returns_the_newly_confirmed_record(fx, db_session):
    pending = submit_authorization_record(
        db_session, HR, fx.high.id, authorization_type="stem_opt", effective_from=TODAY,
    )
    confirm_authorization_record(db_session, HR, pending.id)

    current = _get_current_authorization_record(db_session, fx.high.id)
    assert current.id == pending.id


def test_confirm_first_ever_record_has_no_supersedes(fx, db_session):
    pending = submit_authorization_record(
        db_session, HR, fx.no_record.id, authorization_type="citizen", effective_from=TODAY,
    )
    confirmed = confirm_authorization_record(db_session, HR, pending.id)
    assert confirmed.is_current is True
    row = db_session.get(WorkAuthorizationRecord, confirmed.id)
    assert row.supersedes_record_id is None


def test_confirm_never_leaves_two_current_rows_for_one_employee(fx, db_session):
    pending = submit_authorization_record(
        db_session, HR, fx.high.id, authorization_type="stem_opt", effective_from=TODAY,
    )
    confirm_authorization_record(db_session, HR, pending.id)

    current_count = (
        db_session.query(WorkAuthorizationRecord)
        .filter(WorkAuthorizationRecord.employee_id == fx.high.id, WorkAuthorizationRecord.is_current == True)  # noqa: E712
        .count()
    )
    assert current_count == 1


def test_partial_unique_index_rejects_two_current_rows_directly(fx, db_session):
    """Not exercising confirm_authorization_record at all -- proves the DB
    constraint confirm's flush ordering depends on is actually live. If
    this test ever stops raising, the ordering in confirm_authorization_record
    stops being a safety net for anything."""
    db_session.add(WorkAuthorizationRecord(
        employee_id=fx.high.id, authorization_type=WorkAuthorizationType.opt,
        effective_from=TODAY, verification_status=VerificationStatus.verified,
        is_current=True,
    ))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_confirm_stale_pending_record_supersedes_whatever_is_actually_current(fx, db_session):
    """Two pending submissions for the same employee, confirmed out of
    order. pending_b is confirmed first and becomes current; pending_a,
    confirmed second, must supersede pending_b -- not the original record
    that was current when pending_a was submitted -- because
    supersedes_record_id is computed fresh at confirm time."""
    original = _get_current_authorization_record(db_session, fx.high.id)
    pending_a = submit_authorization_record(
        db_session, HR, fx.high.id, authorization_type="stem_opt", effective_from=TODAY,
    )
    pending_b = submit_authorization_record(
        db_session, HR, fx.high.id, authorization_type="h1b", effective_from=TODAY,
    )

    confirmed_b = confirm_authorization_record(db_session, HR, pending_b.id)
    confirmed_a = confirm_authorization_record(db_session, HR, pending_a.id)

    assert db_session.get(WorkAuthorizationRecord, confirmed_b.id).supersedes_record_id == original.id
    assert db_session.get(WorkAuthorizationRecord, confirmed_a.id).supersedes_record_id == confirmed_b.id
    assert _get_current_authorization_record(db_session, fx.high.id).id == confirmed_a.id


def test_confirm_unknown_record_raises_not_found(fx, db_session):
    with pytest.raises(AuthorizationRecordNotFound):
        confirm_authorization_record(db_session, HR, 999999999)


def test_confirm_already_verified_record_raises_not_actionable(fx, db_session):
    already_verified = _get_current_authorization_record(db_session, fx.history_emp.id)
    with pytest.raises(AuthorizationRecordNotActionable):
        confirm_authorization_record(db_session, HR, already_verified.id)


def test_double_confirm_raises_not_actionable(fx, db_session):
    pending = submit_authorization_record(
        db_session, HR, fx.high.id, authorization_type="stem_opt", effective_from=TODAY,
    )
    confirm_authorization_record(db_session, HR, pending.id)
    with pytest.raises(AuthorizationRecordNotActionable):
        confirm_authorization_record(db_session, HR, pending.id)


@pytest.mark.parametrize("caller", [EMPLOYEE_CALLER, MANAGER_CALLER])
def test_confirm_forbidden_for_non_hr(fx, db_session, caller):
    pending = submit_authorization_record(
        db_session, HR, fx.high.id, authorization_type="stem_opt", effective_from=TODAY,
    )
    with pytest.raises(ContinuityForbidden):
        confirm_authorization_record(db_session, caller, pending.id)


def test_confirm_writes_audit_row(fx, db_session):
    pending = submit_authorization_record(
        db_session, HR, fx.high.id, authorization_type="stem_opt", effective_from=TODAY,
    )
    before = db_session.query(AuditLog).filter_by(actor_id=HR.id).count()
    confirm_authorization_record(db_session, HR, pending.id)
    after = db_session.query(AuditLog).filter_by(actor_id=HR.id).count()
    assert after == before + 1

    row = db_session.query(AuditLog).filter_by(actor_id=HR.id).order_by(AuditLog.id.desc()).first()
    assert row.action == "continuity.authorization_confirmed"


def test_reject_marks_rejected_and_keeps_the_row(fx, db_session):
    pending = submit_authorization_record(
        db_session, HR, fx.high.id, authorization_type="stem_opt", effective_from=TODAY,
    )
    rejected = reject_authorization_record(db_session, HR, pending.id)

    assert rejected.verification_status == "rejected"
    row = db_session.get(WorkAuthorizationRecord, rejected.id)
    assert row is not None  # kept, not deleted
    assert row.is_current is False
    assert row.verified_by is None
    assert row.verified_at is None


def test_reject_does_not_disturb_the_existing_current_record(fx, db_session):
    before = _get_current_authorization_record(db_session, fx.high.id)
    pending = submit_authorization_record(
        db_session, HR, fx.high.id, authorization_type="stem_opt", effective_from=TODAY,
    )
    reject_authorization_record(db_session, HR, pending.id)

    after = _get_current_authorization_record(db_session, fx.high.id)
    assert after.id == before.id


def test_confirm_after_reject_raises_not_actionable(fx, db_session):
    pending = submit_authorization_record(
        db_session, HR, fx.high.id, authorization_type="stem_opt", effective_from=TODAY,
    )
    reject_authorization_record(db_session, HR, pending.id)
    with pytest.raises(AuthorizationRecordNotActionable):
        confirm_authorization_record(db_session, HR, pending.id)


def test_reject_unknown_record_raises_not_found(fx, db_session):
    with pytest.raises(AuthorizationRecordNotFound):
        reject_authorization_record(db_session, HR, 999999999)


@pytest.mark.parametrize("caller", [EMPLOYEE_CALLER, MANAGER_CALLER])
def test_reject_forbidden_for_non_hr(fx, db_session, caller):
    pending = submit_authorization_record(
        db_session, HR, fx.high.id, authorization_type="stem_opt", effective_from=TODAY,
    )
    with pytest.raises(ContinuityForbidden):
        reject_authorization_record(db_session, caller, pending.id)


def test_reject_writes_audit_row(fx, db_session):
    pending = submit_authorization_record(
        db_session, HR, fx.high.id, authorization_type="stem_opt", effective_from=TODAY,
    )
    before = db_session.query(AuditLog).filter_by(actor_id=HR.id).count()
    reject_authorization_record(db_session, HR, pending.id)
    after = db_session.query(AuditLog).filter_by(actor_id=HR.id).count()
    assert after == before + 1

    row = db_session.query(AuditLog).filter_by(actor_id=HR.id).order_by(AuditLog.id.desc()).first()
    assert row.action == "continuity.authorization_rejected"
