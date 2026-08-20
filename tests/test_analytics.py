"""Tests for app/analytics.py — the HR and manager dashboards.

Three things are worth testing here and everything else follows from them:

  SCOPE      who a dashboard is about, and that no request parameter can
             widen it. This is the module's only authorization decision, so
             it gets the most tests.
  BUCKETS    the training split, which has to be mutually exclusive and to
             treat a missing deadline as "not late" rather than as late.
  VERDICTS   the supply/demand classification, which is pure arithmetic over
             two counts and is where an off-by-one silently becomes a
             staffing decision.

Fixture data is created and torn down per test function, isolated by a
distinctive id/name prefix, same pattern as tests/test_project_skills.py and
tests/test_continuity.py.
"""
from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from app.analytics import (
    DashboardForbidden, insights, overview, project_coverage, resolve_scope,
    send_reminders, skill_detail, skill_supply_demand, training_analytics, training_roster,
)
from app.auth import AuthenticatedUser
from app.models import (
    CourseRequirement, Employee, EmployeeCourseStatus, EmployeeProject, EmployeeSkill,
    Notification, Office, OrgUnit, Project, ProjectSkillRequirement, Skill, TrainingCourse,
)
from app.models.enums import (
    AvailabilityStatus, CourseStatus, EmploymentType, ProjectClassification, ProjectType,
    SkillCategory, SkillLevel, SkillSource,
)
from tests.conftest import auth_headers

PREFIX = "analytics-fixture-"
NAME = "Analytics Fixture"

HR = AuthenticatedUser(id=f"{PREFIX}hr-caller", role="hr", name="Test HR")
TODAY = date(2026, 6, 15)


def _mkemp(db, key, org_unit_id, office_id, **overrides):
    fields = dict(
        id=f"{PREFIX}{key}", directory_object_id=None, full_name=f"{NAME} {key}", preferred_name=None,
        job_title="Consultant", org_unit_id=org_unit_id, office_id=office_id, manager_id=None,
        work_email=f"{PREFIX}{key}@example.test", work_phone=None, slack_handle=None, timezone=None,
        employment_type=EmploymentType.fte, hire_date=date(2020, 1, 1), cost_centre=None,
        personal_mobile=None, availability_status=AvailabilityStatus.available,
        away_until=None, delegate_id=None, bio=None, photo_url=None, is_active=True,
    )
    fields.update(overrides)
    emp = Employee(**fields)
    db.add(emp)
    return emp


@pytest.fixture
def fx(db_session):
    """A two-team org under one manager, with a skill, a project and a course.

    Deliberately small and hand-checkable: every count asserted below can be
    read off this function. The tests run against the seeded demo database,
    so assertions are scoped to the fixture's own manager rather than to the
    whole org — an org-wide count would move whenever the seed does.
    """
    db = db_session
    office = db.query(Office).first()
    unit = OrgUnit(name=f"{NAME} Unit", parent_id=None, unit_type="department")
    child = OrgUnit(name=f"{NAME} Child Unit", parent_id=None, unit_type="team")
    db.add_all([unit, child])
    db.flush()
    child.parent_id = unit.id
    db.flush()

    boss = _mkemp(db, "boss", unit.id, office.id, job_title="Fixture Manager")
    db.flush()
    # Two direct reports in the parent unit, one indirect report under a
    # child unit -- so the subtree walk and the org-unit descent are both
    # exercised, and by different data.
    a = _mkemp(db, "a", unit.id, office.id, manager_id=boss.id)
    b = _mkemp(db, "b", unit.id, office.id, manager_id=boss.id)
    db.flush()
    c = _mkemp(db, "c", child.id, office.id, manager_id=a.id)
    outsider = _mkemp(db, "outsider", unit.id, office.id)
    db.flush()

    skill = Skill(name=f"{NAME} Skill", category=SkillCategory.technical, canonical_id=None)
    spare = Skill(name=f"{NAME} Spare Skill", category=SkillCategory.technical, canonical_id=None)
    db.add_all([skill, spare])
    db.flush()

    # a: Expert, b: Working, c: Learning -> capable = 2, holders = 3.
    for emp, level in ((a, SkillLevel.expert), (b, SkillLevel.working), (c, SkillLevel.learning)):
        db.add(EmployeeSkill(employee_id=emp.id, skill_id=skill.id, level=level,
                             source=SkillSource.confirmed, verified_at=None))

    project = Project(
        name=f"{NAME} Project", type=ProjectType.project, description="",
        owning_unit_id=unit.id, owner_id=boss.id, classification=ProjectClassification.internal,
        is_client_engagement=False,
    )
    db.add(project)
    db.flush()
    # a and b are currently on it; c is not.
    for emp in (a, b):
        db.add(EmployeeProject(employee_id=emp.id, project_id=project.id, role="Engineer",
                               contribution=None, start_date=date(2024, 1, 1), end_date=None))
    db.add(ProjectSkillRequirement(project_id=project.id, skill_id=skill.id,
                                   minimum_level=SkillLevel.working))

    course = TrainingCourse(code=f"{PREFIX}COURSE", name=f"{NAME} Course", description=None, is_active=True)
    db.add(course)
    db.flush()
    # Scoped to the fixture unit so it never attaches to seeded employees.
    requirement = CourseRequirement(
        course_id=course.id, org_unit_id=unit.id, job_title_keyword=None, employment_type=None,
        due_date=TODAY - timedelta(days=10), due_days_after_hire=None, note=None,
    )
    db.add(requirement)
    db.flush()

    # a completed it; b failed it; c has no row at all (read as not started).
    db.add(EmployeeCourseStatus(employee_id=a.id, course_id=course.id, status=CourseStatus.completed,
                                attempted_at=date(2026, 1, 1), completed_at=date(2026, 1, 2),
                                source="synthetic", last_synced_at=None))
    db.add(EmployeeCourseStatus(employee_id=b.id, course_id=course.id, status=CourseStatus.failed,
                                attempted_at=date(2026, 1, 1), completed_at=None,
                                source="synthetic", last_synced_at=None))
    db.commit()

    yield SimpleNamespace(unit=unit, child=child, boss=boss, a=a, b=b, c=c, outsider=outsider,
                          skill=skill, spare=spare, project=project, course=course,
                          requirement=requirement,
                          manager=AuthenticatedUser(id=boss.id, role="manager", name=boss.full_name))

    ids = [boss.id, a.id, b.id, c.id, outsider.id]
    db.query(Notification).filter(Notification.recipient_id.in_(ids)).delete(synchronize_session=False)
    db.query(EmployeeCourseStatus).filter(
        EmployeeCourseStatus.employee_id.in_(ids)).delete(synchronize_session=False)
    db.query(CourseRequirement).filter_by(course_id=course.id).delete(synchronize_session=False)
    db.query(TrainingCourse).filter_by(id=course.id).delete(synchronize_session=False)
    db.query(ProjectSkillRequirement).filter_by(project_id=project.id).delete(synchronize_session=False)
    db.query(EmployeeProject).filter_by(project_id=project.id).delete(synchronize_session=False)
    db.query(Project).filter_by(id=project.id).delete(synchronize_session=False)
    db.query(EmployeeSkill).filter(EmployeeSkill.employee_id.in_(ids)).delete(synchronize_session=False)
    db.query(Skill).filter(Skill.name.like(f"{NAME}%")).delete(synchronize_session=False)
    db.query(Employee).filter(Employee.id.like(f"{PREFIX}%")).delete(synchronize_session=False)
    db.query(OrgUnit).filter(OrgUnit.name.like(f"{NAME}%")).delete(synchronize_session=False)
    db.commit()


# --- Scope: the module's one authorization decision -------------------------

def test_hr_defaults_to_the_whole_organization(fx, db_session):
    scope = resolve_scope(db_session, HR, "work")
    assert scope.kind == "org"
    assert fx.outsider.id in scope.employee_ids


def test_hr_can_scope_to_an_org_unit_subtree(fx, db_session):
    scope = resolve_scope(db_session, HR, "work", org_unit_id=fx.unit.id)
    assert scope.kind == "org_unit"
    # The child unit's employee is included: employees only ever sit in their
    # most-specific unit, so a department filter that didn't descend would
    # return nobody from the teams under it.
    assert {fx.boss.id, fx.a.id, fx.b.id, fx.c.id, fx.outsider.id} == set(scope.employee_ids)


def test_hr_can_scope_to_any_managers_line(fx, db_session):
    scope = resolve_scope(db_session, HR, "work", manager_id=fx.boss.id)
    assert scope.kind == "team"
    # The manager themself is excluded, and so is the unrelated colleague in
    # the same unit -- this is a reporting line, not a unit.
    assert {fx.a.id, fx.b.id, fx.c.id} == set(scope.employee_ids)


def test_hr_in_employee_mode_gets_no_dashboard(fx, db_session):
    """Employee mode is 'what an ordinary colleague sees', and an org-wide
    compliance dashboard is precisely what an ordinary colleague does not."""
    with pytest.raises(DashboardForbidden):
        resolve_scope(db_session, HR, "employee")


def test_manager_gets_their_own_reporting_subtree(fx, db_session):
    scope = resolve_scope(db_session, fx.manager, "employee")
    assert scope.kind == "team"
    assert {fx.a.id, fx.b.id, fx.c.id} == set(scope.employee_ids)
    assert scope.substituted is False


@pytest.mark.parametrize("kwargs", [
    {"org_unit_id": 1},
    {"manager_id": "somebody-else"},
    {"org_unit_id": 1, "manager_id": "somebody-else"},
])
def test_manager_cannot_widen_their_scope_with_any_parameter(fx, db_session, kwargs):
    """The requested scope is not validated against a manager's permissions,
    it is DISCARDED — so there is no combination of parameters that reaches
    further than their own line."""
    scope = resolve_scope(db_session, fx.manager, "employee", **kwargs)
    assert {fx.a.id, fx.b.id, fx.c.id} == set(scope.employee_ids)
    assert scope.substituted is True


def test_manager_scope_is_identical_in_work_mode(fx, db_session):
    """A manager cannot reach work mode server-side anyway, but the scope
    must not depend on the mode they claim."""
    assert (set(resolve_scope(db_session, fx.manager, "work").employee_ids)
            == set(resolve_scope(db_session, fx.manager, "employee").employee_ids))


def test_employee_with_no_reports_has_no_dashboard(fx, db_session):
    caller = AuthenticatedUser(id=fx.outsider.id, role="employee", name="Nobody")
    with pytest.raises(DashboardForbidden):
        resolve_scope(db_session, caller, "employee")


def test_manager_role_claim_without_reports_is_still_refused(fx, db_session):
    """Keyed on having reports, not on the role claim: the claim arrives on
    the request, the reports are a fact about the data."""
    caller = AuthenticatedUser(id=fx.outsider.id, role="manager", name="Claims To Manage")
    with pytest.raises(DashboardForbidden):
        resolve_scope(db_session, caller, "employee")


def test_it_role_has_no_dashboard(fx, db_session):
    caller = AuthenticatedUser(id=fx.outsider.id, role="it", name="IT")
    with pytest.raises(DashboardForbidden):
        resolve_scope(db_session, caller, "work")


# --- Supply vs demand -------------------------------------------------------

def _row_for(rows, skill_id):
    return next(r for r in rows if r.skill_id == skill_id)


def test_supply_counts_capable_separately_from_holders(fx, db_session):
    row = _row_for(skill_supply_demand(db_session, HR, "work", manager_id=fx.boss.id), fx.skill.id)
    assert (row.expert_count, row.working_count, row.learning_count) == (1, 1, 1)
    # Capable is Expert + Working: a Learning holder is real but is not
    # somebody a project can be staffed on.
    assert row.capable_count == 2
    assert row.holder_count == 3


def test_declared_requirement_is_reported_as_declared_demand(fx, db_session):
    row = _row_for(skill_supply_demand(db_session, HR, "work", manager_id=fx.boss.id), fx.skill.id)
    assert row.demand_project_count == 1
    assert row.demand_basis == "declared"
    assert row.declared_project_count == 1


def test_two_capable_against_one_project_is_healthy(fx, db_session):
    row = _row_for(skill_supply_demand(db_session, HR, "work", manager_id=fx.boss.id), fx.skill.id)
    assert row.verdict == "healthy"
    assert row.single_point_of_failure is False


def test_a_single_capable_person_against_real_demand_is_a_single_point(fx, db_session):
    """A ratio of 1.0 scores 'healthy' on arithmetic alone and is still one
    resignation from a gap, which is why this is a separate flag."""
    db_session.query(EmployeeSkill).filter_by(
        employee_id=fx.b.id, skill_id=fx.skill.id).delete(synchronize_session=False)
    db_session.commit()
    row = _row_for(skill_supply_demand(db_session, HR, "work", manager_id=fx.boss.id), fx.skill.id)
    assert row.capable_count == 1
    assert row.single_point_of_failure is True


def test_supply_and_demand_both_move_with_the_scope(fx, db_session):
    """The same function serves an org-wide view and one team; the numbers
    must mean the same thing in each, which means both sides are scoped."""
    team = _row_for(skill_supply_demand(db_session, HR, "work", manager_id=fx.boss.id), fx.skill.id)
    db_session.add(EmployeeSkill(employee_id=fx.outsider.id, skill_id=fx.skill.id,
                                 level=SkillLevel.expert, source=SkillSource.confirmed, verified_at=None))
    db_session.commit()
    unit = _row_for(skill_supply_demand(db_session, HR, "work", org_unit_id=fx.unit.id), fx.skill.id)
    # The outsider is in the unit but not in the reporting line.
    assert unit.capable_count == team.capable_count + 1


# --- Skill detail (the popup) ----------------------------------------------

def test_skill_detail_reports_holders_projects_and_risk(fx, db_session):
    detail = skill_detail(db_session, HR, fx.skill.id, "work", manager_id=fx.boss.id)
    assert detail is not None
    assert detail.holder_count == 3
    assert {h.id for h in detail.holders} == {fx.a.id, fx.b.id, fx.c.id}
    assert [p.project_id for p in detail.projects] == [fx.project.id]
    assert detail.projects[0].basis == "declared"
    assert detail.projects[0].capable_member_count == 2
    # Every risk verdict states the count that produced it.
    assert detail.risk_reason


def test_skill_detail_is_none_for_a_skill_absent_from_the_scope(fx, db_session):
    """A 404 rather than an all-zeros card: a card full of zeros looks like a
    measurement, and this is an absence."""
    assert skill_detail(db_session, HR, fx.spare.id, "work", manager_id=fx.boss.id) is None


def test_skill_detail_respects_a_managers_scope(fx, db_session):
    db_session.add(EmployeeSkill(employee_id=fx.outsider.id, skill_id=fx.skill.id,
                                 level=SkillLevel.expert, source=SkillSource.confirmed, verified_at=None))
    db_session.commit()
    detail = skill_detail(db_session, fx.manager, fx.skill.id, "employee")
    assert detail is not None
    assert fx.outsider.id not in {h.id for h in detail.holders}


# --- Training buckets -------------------------------------------------------

def _buckets(db, **kw):
    return training_analytics(db, HR, "work", manager_id=kw.pop("manager_id"), today=TODAY, **kw).buckets


def test_buckets_are_mutually_exclusive_and_sum_to_expected(fx, db_session):
    b = _buckets(db_session, manager_id=fx.boss.id)
    assert b.completed + b.overdue + b.due_soon + b.outstanding == b.expected
    # `incomplete` is the rollup and deliberately overlaps the other three.
    assert b.incomplete == b.overdue + b.due_soon + b.outstanding


def test_a_past_deadline_puts_incomplete_people_in_overdue(fx, db_session):
    b = _buckets(db_session, manager_id=fx.boss.id)
    assert b.expected == 3           # a, b and c are all expected to take it
    assert b.completed == 1          # a
    assert b.overdue == 2            # b failed it, c has no record at all
    assert b.compliance_pct == pytest.approx(33.3, abs=0.1)


def test_a_missing_status_row_is_read_as_not_started_and_counted(fx, db_session):
    result = training_analytics(db_session, HR, "work", manager_id=fx.boss.id, today=TODAY)
    # c has no row. Reported separately so the UI can qualify the figure
    # rather than present an inference from absence as a measurement.
    assert result.no_record_count == 1


def test_a_requirement_with_no_deadline_is_never_overdue(fx, db_session):
    """Nothing in the data says when it was due, so nothing here decides it
    was late -- it lands in `outstanding`, which is not a compliance failure."""
    fx.requirement.due_date = None
    fx.requirement.due_days_after_hire = None
    db_session.commit()
    result = training_analytics(db_session, HR, "work", manager_id=fx.boss.id, today=TODAY)
    assert result.buckets.overdue == 0
    assert result.buckets.outstanding == 2
    assert result.no_deadline_count == 3


def test_a_deadline_inside_the_window_is_due_soon(fx, db_session):
    fx.requirement.due_date = TODAY + timedelta(days=5)
    db_session.commit()
    b = _buckets(db_session, manager_id=fx.boss.id)
    assert (b.overdue, b.due_soon) == (0, 2)


def test_a_deadline_beyond_the_window_is_merely_outstanding(fx, db_session):
    fx.requirement.due_date = TODAY + timedelta(days=120)
    db_session.commit()
    b = _buckets(db_session, manager_id=fx.boss.id)
    assert (b.overdue, b.due_soon, b.outstanding) == (0, 0, 2)


def test_hire_relative_deadline_extends_rather_than_tightens(fx, db_session):
    """due_days_after_hire is a joining grace period, not a second deadline
    to also beat: a person hired six years ago must not be retroactively late
    by their long-past hire+60."""
    fx.requirement.due_date = TODAY + timedelta(days=5)
    fx.requirement.due_days_after_hire = 60      # 2020-01-01 + 60d, long gone
    db_session.commit()
    b = _buckets(db_session, manager_id=fx.boss.id)
    assert b.overdue == 0
    assert b.due_soon == 2


# --- Roster and reminders ---------------------------------------------------

def test_roster_reports_the_two_value_display_status_only(fx, db_session):
    roster = training_roster(db_session, HR, "work", manager_id=fx.boss.id,
                             bucket="overdue", today=TODAY)
    assert {r.employee_id for r in roster.rows} == {fx.b.id, fx.c.id}
    # b FAILED and c never started; both render as "not_completed". Which of
    # the two somebody sits in decides their reminder wording and is not
    # management-facing data.
    assert {r.display_status for r in roster.rows} == {"not_completed"}
    assert all(r.days_overdue == 10 for r in roster.rows)


def test_reminders_reach_the_selected_people(fx, db_session):
    result = send_reminders(db_session, HR, "work", employee_ids=[fx.b.id, fx.c.id],
                            manager_id=fx.boss.id, today=TODAY)
    assert result.sent == 2
    assert result.recipients_notified == 2
    bodies = db_session.query(Notification).filter(
        Notification.recipient_id.in_([fx.b.id, fx.c.id])).all()
    # The deadline is stated, which the automatic status-change trigger
    # cannot do -- it fires before any due date is in scope.
    assert all("due" in n.body for n in bodies)


def test_a_completed_course_is_never_reminded_about(fx, db_session):
    result = send_reminders(db_session, HR, "work", employee_ids=[fx.a.id],
                            manager_id=fx.boss.id, today=TODAY)
    assert result.eligible == 0
    assert result.sent == 0


def test_a_second_reminder_the_same_day_is_suppressed(fx, db_session):
    first = send_reminders(db_session, HR, "work", employee_ids=[fx.b.id],
                           manager_id=fx.boss.id, today=TODAY)
    second = send_reminders(db_session, HR, "work", employee_ids=[fx.b.id],
                            manager_id=fx.boss.id, today=TODAY)
    assert (first.sent, second.sent) == (1, 0)
    assert second.skipped == 1


def test_ids_outside_the_callers_scope_are_dropped_and_counted(fx, db_session):
    """A stale selection can never reach further than the dashboard that
    produced it. Dropped, not rejected -- redact, never reject."""
    result = send_reminders(db_session, fx.manager, "employee",
                            employee_ids=[fx.b.id, fx.outsider.id], today=TODAY)
    assert result.out_of_scope == 1
    assert not db_session.query(Notification).filter_by(recipient_id=fx.outsider.id).count()


# --- Project coverage -------------------------------------------------------

def test_coverage_judges_only_projects_with_declared_requirements(fx, db_session):
    row = next(p for p in project_coverage(db_session, HR, "work", manager_id=fx.boss.id)
               if p.project_id == fx.project.id)
    assert row.requirements_recorded is True
    assert (row.covered_skill_count, row.required_skill_count) == (1, 1)
    assert row.coverage_pct == 100.0
    assert row.gap_skills == []


def test_a_requirement_no_current_member_meets_is_a_gap(fx, db_session):
    db_session.query(EmployeeSkill).filter(
        EmployeeSkill.employee_id.in_([fx.a.id, fx.b.id]),
        EmployeeSkill.skill_id == fx.skill.id,
    ).delete(synchronize_session=False)
    db_session.commit()
    row = next(p for p in project_coverage(db_session, HR, "work", manager_id=fx.boss.id)
               if p.project_id == fx.project.id)
    assert row.gap_skills == [fx.skill.name]
    assert row.risk == "high"


def test_a_project_with_no_declared_requirements_gets_no_verdict(fx, db_session):
    """Inferring a project's needs from its members and then checking its
    members against them would report full coverage everywhere."""
    db_session.query(ProjectSkillRequirement).filter_by(
        project_id=fx.project.id).delete(synchronize_session=False)
    db_session.commit()
    row = next(p for p in project_coverage(db_session, HR, "work", manager_id=fx.boss.id)
               if p.project_id == fx.project.id)
    assert row.requirements_recorded is False
    assert row.coverage_pct is None
    assert row.risk == "unknown"


# --- Overview and insights --------------------------------------------------

def test_overview_agrees_with_the_sections_beneath_it(fx, db_session):
    ov = overview(db_session, HR, "work", manager_id=fx.boss.id, today=TODAY)
    rows = skill_supply_demand(db_session, HR, "work", manager_id=fx.boss.id)
    assert ov.headcount == 3
    assert ov.active_project_count == 1
    assert ov.training.expected == training_analytics(
        db_session, HR, "work", manager_id=fx.boss.id, today=TODAY).buckets.expected
    assert ov.understaffed_skill_count == sum(1 for r in rows if r.verdict == "understaffed")


def test_insights_state_the_counts_behind_each_claim(fx, db_session):
    found = insights(db_session, HR, "work", manager_id=fx.boss.id, today=TODAY)
    compliance = next(i for i in found if i.kind == "training_compliance")
    assert "2" in compliance.title
    assert compliance.evidence          # names the units the overdue sit in
    assert compliance.recommendation


def test_insights_are_omitted_rather_than_padded(fx, db_session):
    """When nothing crosses a threshold the section is shorter, not filled
    with observations."""
    fx.requirement.due_date = TODAY + timedelta(days=365)
    db_session.commit()
    kinds = {i.kind for i in insights(db_session, HR, "work", manager_id=fx.boss.id, today=TODAY)}
    assert "training_compliance" not in kinds


# --- HTTP layer -------------------------------------------------------------

async def test_routes_refuse_an_ordinary_employee(client, fx):
    for path in ("/analytics/overview", "/analytics/skills", "/analytics/training",
                 "/analytics/projects", "/analytics/insights", "/analytics/org-units"):
        resp = await client.get(path, headers=auth_headers("employee", fx.outsider.id))
        assert resp.status_code == 403, path


async def test_routes_refuse_hr_in_employee_mode(client, fx):
    resp = await client.get("/analytics/overview?view_mode=employee", headers=auth_headers("hr"))
    assert resp.status_code == 403


async def test_manager_route_ignores_a_wider_scope_parameter(client, fx):
    resp = await client.get(f"/analytics/overview?org_unit_id={fx.unit.id}",
                            headers=auth_headers("manager", fx.boss.id))
    assert resp.status_code == 200
    body = resp.json()
    assert body["headcount"] == 3
    assert body["scope"]["substituted"] is True


async def test_reminder_route_refuses_an_ordinary_employee(client, fx):
    resp = await client.post("/analytics/training/reminders",
                             headers=auth_headers("employee", fx.outsider.id),
                             json={"employee_ids": [fx.b.id]})
    assert resp.status_code == 403


async def test_skill_detail_route_404s_outside_the_scope(client, fx):
    resp = await client.get(f"/analytics/skills/{fx.spare.id}?view_mode=work&manager_id={fx.boss.id}",
                            headers=auth_headers("hr"))
    assert resp.status_code == 404
