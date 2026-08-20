"""Tests for app/team_finder.py — Find the Right Team.

What carries the weight:

  BOUNDARY    this feature ranks org-wide on purpose, so "it is allowed to
              look widely" must not become "it leaks". The counts are the
              subtle part: a headcount computed over someone the caller
              cannot see discloses that person without ever naming them.
  GRANULARITY a department is a superset of its teams and therefore wins
              every comparative term by construction. Two tests pin the
              handling, because the naive version answered "which TEAM…"
              with a department every single time.
  ARITHMETIC  relative scoring, level weighting, and the enum-ordering trap
              that makes max() on SkillLevel return "Working".

The model is stubbed off everywhere (autouse). The planner's model branch is
exercised through _need_from_args, which is the part worth testing.
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from app.auth import AuthenticatedUser
from app.models import (
    Employee, EmployeeProject, EmployeeSkill, Office, OrgUnit, Project,
    ProjectSkillRequirement, Skill,
)
from app.models.enums import (
    AvailabilityStatus, EmploymentType, ProjectClassification, ProjectType,
    SkillCategory, SkillLevel, SkillSource,
)
from app.schemas import TeamFindRequest, TeamRecommendationResult
from app.team_finder import (
    _need_from_args, find_teams, load_workforce, preferred_unit_type, read_need, unit_head,
)
from tests.conftest import auth_headers

PREFIX = "tf-fixture-"
NAME = "TF Fixture"

HR = AuthenticatedUser(id=f"{PREFIX}hr-caller", role="hr", name="Test HR")
# NOT the lead. This constant used to be f"{PREFIX}lead", which is the very
# employee the fixture puts on the confidential project -- so the
# "non-member cannot see it" test was being run as a member and passed
# nothing. Points at w2, who is in the other team and on no project.
EMPLOYEE = AuthenticatedUser(id=f"{PREFIX}w2", role="employee", name="Test Employee")


@pytest.fixture(autouse=True)
def no_model(monkeypatch):
    """No test in this file may reach Azure OpenAI."""
    monkeypatch.setattr("app.tool_calling._mode", lambda: "mock")


def _mkemp(db, key, org_unit_id, office_id, **overrides):
    fields = dict(
        id=f"{PREFIX}{key}", directory_object_id=None, full_name=f"{NAME} {key}",
        preferred_name=None, job_title="Engineer", org_unit_id=org_unit_id,
        office_id=office_id, manager_id=None, work_email=f"{PREFIX}{key}@example.test",
        work_phone=None, slack_handle=None, timezone=None,
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
    """One department, two teams under it, and a restricted employee.

        TF Dept
          ├── TF Strong Team    lead(Expert Widget), w1(Working Widget),
          │                     hidden(Expert Widget, RESTRICTED)
          └── TF Weak Team      w2(Learning Widget)

    `hidden` is the whole point of several tests below: they hold the skill
    at Expert, so if the visibility filter runs anywhere later than it
    should, they move a count, a level tally, or a score.
    """
    db = db_session
    office = db.query(Office).first()
    dept = OrgUnit(name=f"{NAME} Dept", parent_id=None, unit_type="department")
    db.add(dept)
    db.flush()
    strong = OrgUnit(name=f"{NAME} Strong Team", parent_id=dept.id, unit_type="team")
    weak = OrgUnit(name=f"{NAME} Weak Team", parent_id=dept.id, unit_type="team")
    db.add_all([strong, weak])
    db.flush()

    lead = _mkemp(db, "lead", strong.id, office.id, job_title=f"{NAME} Strong Team Manager")
    db.flush()
    w1 = _mkemp(db, "w1", strong.id, office.id, manager_id=lead.id)
    hidden = _mkemp(db, "hidden", strong.id, office.id, manager_id=lead.id,
                    availability_status=AvailabilityStatus.restricted)
    w2 = _mkemp(db, "w2", weak.id, office.id, manager_id=lead.id)
    db.flush()

    widget = Skill(name=f"{NAME} Widget", category=SkillCategory.technical, canonical_id=None)
    other = Skill(name=f"{NAME} Other", category=SkillCategory.technical, canonical_id=None)
    # Held one-per-team on purpose. Widget is CONCENTRATED (both visible
    # holders in Strong Team) and Spread is not, so the two branches of
    # _drop_redundant_parents each get a query that actually exercises them.
    spread = Skill(name=f"{NAME} Spread", category=SkillCategory.technical, canonical_id=None)
    db.add_all([widget, other, spread])
    db.flush()

    def hold(emp, skill, level):
        db.add(EmployeeSkill(employee_id=emp.id, skill_id=skill.id, level=level,
                             source=SkillSource.confirmed, verified_at=None))

    hold(lead, widget, SkillLevel.expert)
    hold(w1, widget, SkillLevel.working)
    hold(hidden, widget, SkillLevel.expert)
    hold(w2, widget, SkillLevel.learning)
    hold(lead, spread, SkillLevel.working)
    hold(w2, spread, SkillLevel.working)

    project = Project(
        name=f"{NAME} Widget Programme", type=ProjectType.project, description="",
        owning_unit_id=strong.id, owner_id=lead.id,
        classification=ProjectClassification.internal, is_client_engagement=False)
    secret = Project(
        name=f"{NAME} Secret Programme", type=ProjectType.project, description="",
        owning_unit_id=strong.id, owner_id=lead.id,
        classification=ProjectClassification.confidential, is_client_engagement=False)
    db.add_all([project, secret])
    db.flush()
    for p in (project, secret):
        db.add(EmployeeProject(employee_id=lead.id, project_id=p.id, role="Engineer",
                               contribution=None, start_date=date(2024, 1, 1), end_date=None))
        db.add(ProjectSkillRequirement(project_id=p.id, skill_id=widget.id,
                                       minimum_level=SkillLevel.working))
    db.commit()

    yield SimpleNamespace(dept=dept, strong=strong, weak=weak, lead=lead, w1=w1,
                          hidden=hidden, w2=w2, widget=widget, other=other, spread=spread,
                          project=project, secret=secret)

    ids = [lead.id, w1.id, hidden.id, w2.id]
    for p in (project, secret):
        db.query(ProjectSkillRequirement).filter_by(project_id=p.id).delete(synchronize_session=False)
        db.query(EmployeeProject).filter_by(project_id=p.id).delete(synchronize_session=False)
    db.query(Project).filter(Project.name.like(f"{NAME}%")).delete(synchronize_session=False)
    db.query(EmployeeSkill).filter(EmployeeSkill.employee_id.in_(ids)).delete(synchronize_session=False)
    db.query(Skill).filter(Skill.name.like(f"{NAME}%")).delete(synchronize_session=False)
    db.query(Employee).filter(Employee.id.like(f"{PREFIX}%")).delete(synchronize_session=False)
    db.query(OrgUnit).filter(OrgUnit.name.like(f"{NAME}%")).delete(synchronize_session=False)
    db.commit()


def _team(result: TeamRecommendationResult, unit_name: str):
    return next((t for t in result.teams if t.name == unit_name), None)


# ---------------------------------------------------------------------------
# Permission boundaries
# ---------------------------------------------------------------------------

def test_a_restricted_employee_is_absent_from_the_counts(fx, db_session):
    """Not just from the names -- from the COUNTS.

    `hidden` holds Widget at Expert. The Strong Team therefore has two
    Experts in the database and exactly one that an ordinary caller may
    know about. A count of two would disclose a person the caller cannot
    see, without ever printing their name.
    """
    result = find_teams(db_session, EMPLOYEE, f"Which team knows {NAME} Widget?")
    strong = _team(result, f"{NAME} Strong Team")
    assert strong is not None
    widget = next(s for s in strong.skills if s.skill == f"{NAME} Widget")
    assert widget.expert == 1, "the restricted Expert must not be counted"
    assert widget.total == 2
    assert strong.headcount == 2, "restricted people are not in the headcount either"


def test_hr_in_work_mode_does_see_the_restricted_employee(fx, db_session):
    """The mirror of the test above -- otherwise it would pass just as well
    if the fixture data were wrong."""
    result = find_teams(db_session, HR, f"Which team knows {NAME} Widget?", "work")
    strong = _team(result, f"{NAME} Strong Team")
    widget = next(s for s in strong.skills if s.skill == f"{NAME} Widget")
    assert widget.expert == 2
    assert strong.headcount == 3


def test_hr_loses_that_exemption_in_employee_mode(fx, db_session):
    result = find_teams(db_session, HR, f"Which team knows {NAME} Widget?", "employee")
    strong = _team(result, f"{NAME} Strong Team")
    widget = next(s for s in strong.skills if s.skill == f"{NAME} Widget")
    assert widget.expert == 1


def test_a_confidential_project_is_not_listed_to_a_non_member(fx, db_session):
    """Project visibility is a members-only rule and says nothing about
    employee visibility, so it gets its own check rather than riding on
    is_record_visible."""
    result = find_teams(db_session, EMPLOYEE, f"Which team knows {NAME} Widget?")
    strong = _team(result, f"{NAME} Strong Team")
    assert fx.project.name in strong.projects
    assert fx.secret.name not in strong.projects


def test_a_member_does_see_their_own_confidential_project(fx, db_session):
    member = AuthenticatedUser(id=fx.lead.id, role="employee", name="Lead")
    result = find_teams(db_session, member, f"Which team knows {NAME} Widget?")
    strong = _team(result, f"{NAME} Strong Team")
    assert fx.secret.name in strong.projects


def test_an_ordinary_employee_can_search_the_whole_organization(fx, db_session):
    """The deliberate difference from Build Team.

    Build Team uses resolve_scope, which confines a manager to their own
    line. This feature must not: "which other team should I talk to" is
    unanswerable inside your own reporting line. An ordinary employee with
    no reports gets results here and would get a 403 from /team/build.
    """
    nobody = AuthenticatedUser(id=fx.w2.id, role="employee", name="Nobody")
    result = find_teams(db_session, nobody, f"Which team knows {NAME} Widget?")
    assert result.teams, "an ordinary employee must be able to find a team to ask"
    assert any(t.name == f"{NAME} Strong Team" for t in result.teams), \
        "including a team they are not in"


def test_the_recommendation_carries_no_gated_field(fx, db_session):
    from app.schemas import TeamManagerRef
    gated = {"salary", "salary_currency", "date_of_birth", "hire_date",
             "cost_centre", "personal_mobile", "training_status", "availability_status"}
    assert not (set(TeamManagerRef.model_fields) & gated)


def test_contact_details_are_the_real_authorized_ones(fx, db_session):
    result = find_teams(db_session, EMPLOYEE, f"Which team knows {NAME} Widget?")
    strong = _team(result, f"{NAME} Strong Team")
    assert strong.manager is not None
    assert strong.manager.full_name == fx.lead.full_name
    assert strong.manager.work_email == fx.lead.work_email


# ---------------------------------------------------------------------------
# It recommends, it does not create
# ---------------------------------------------------------------------------

def test_every_recommended_unit_already_exists(fx, db_session):
    from sqlalchemy import select
    result = find_teams(db_session, HR, f"Which team knows {NAME} Widget?", "work")
    real = {u.id for u in db_session.execute(select(OrgUnit)).scalars().all()}
    assert result.teams
    for t in result.teams:
        assert t.org_unit_id in real


def test_finding_a_team_writes_nothing(fx, db_session):
    from sqlalchemy import func, select
    before = db_session.execute(select(func.count()).select_from(OrgUnit)).scalar_one()
    emp_before = db_session.execute(select(func.count()).select_from(Employee)).scalar_one()
    find_teams(db_session, HR, f"Which team knows {NAME} Widget?", "work")
    assert db_session.execute(select(func.count()).select_from(OrgUnit)).scalar_one() == before
    assert db_session.execute(select(func.count()).select_from(Employee)).scalar_one() == emp_before


# ---------------------------------------------------------------------------
# Granularity. A department is a superset of its teams.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("query,expected", [
    ("Which team has the strongest Kubernetes expertise?", "team"),
    ("Which team should I talk to about an Azure networking problem?", "team"),
    ("Which department has the most data engineering experience?", "department"),
    ("Which division owns identity?", "department"),
    ("who knows about azure", None),
    ("Which team or department knows Terraform?", None),
])
def test_the_asked_for_granularity_is_recognised(query, expected):
    assert preferred_unit_type(query) == expected


def test_a_team_question_is_answered_with_a_team(fx, db_session):
    """The regression this exists for: a department aggregates its teams and
    so beats every one of them on depth, breadth and project count by
    construction. All three of the specified example queries came back with
    departments before this -- including the two that say "team"."""
    result = find_teams(db_session, HR, f"Which team knows {NAME} Widget?", "work")
    assert result.preferred_unit_type == "team"
    assert result.teams[0].unit_type == "team"


def test_a_department_question_is_answered_with_a_department(fx, db_session):
    result = find_teams(db_session, HR, f"Which department knows {NAME} Spread?", "work")
    assert result.preferred_unit_type == "department"
    assert result.teams[0].unit_type == "department"


def test_the_other_granularity_is_still_offered_below(fx, db_session):
    """A preference, not a filter -- sometimes the wider answer is the right
    one even when someone typed "team", and hiding it decides that for them."""
    # Spread is held one-per-team, so the department is NOT just one team
    # and legitimately stands alongside them.
    result = find_teams(db_session, HR, f"Which team knows {NAME} Spread?", "work")
    assert {t.unit_type for t in result.teams} >= {"team", "department"}


def test_a_department_carried_by_one_team_is_suppressed(fx, db_session):
    """Every Widget holder bar one sits in Strong Team, so recommending the
    department is the same advice one level vaguer.

    The earlier version of this rule compared SCORES and was dead code: a
    department scores at least as high as each of its teams on every
    comparative term, so "a child scored higher" could never fire.
    """
    from app.team_finder import _drop_redundant_parents, _apply_relative_scores

    # Strong Team holds 2 of the department's 3 visible Widget people (67%),
    # which is over the concentration threshold.
    result = find_teams(db_session, HR, f"Who knows {NAME} Widget?", "work")
    names = [t.name for t in result.teams]
    assert f"{NAME} Strong Team" in names
    assert f"{NAME} Dept" not in names, "the department is just Strong Team, blurrier"


# ---------------------------------------------------------------------------
# Scoring arithmetic
# ---------------------------------------------------------------------------

def test_max_on_the_level_enum_would_pick_the_wrong_best(fx):
    """Pinning the trap rather than the workaround. SkillLevel is a str enum,
    so max() compares alphabetically and calls Working the highest level."""
    assert max([SkillLevel.expert, SkillLevel.working, SkillLevel.learning]) is SkillLevel.working


def test_the_best_level_is_chosen_by_weight_not_alphabetically(fx, db_session):
    """Strong Team has an Expert and a Working holder. If the best level were
    picked alphabetically the team would score as a Working team."""
    result = find_teams(db_session, HR, f"Which team knows {NAME} Widget?", "work")
    strong = _team(result, f"{NAME} Strong Team")
    weak = _team(result, f"{NAME} Weak Team")
    assert strong.match_pct > weak.match_pct


def test_expert_depth_outranks_a_learning_only_team(fx, db_session):
    result = find_teams(db_session, EMPLOYEE, f"Which team knows {NAME} Widget?")
    order = [t.name for t in result.teams if t.unit_type == "team"]
    assert order.index(f"{NAME} Strong Team") < order.index(f"{NAME} Weak Team")


def test_scores_differentiate_rather_than_all_saturating(fx, db_session):
    """Absolute thresholds were tried first and produced 100% for the top
    three on every single-skill query, which makes the number useless and
    hands the ordering to a tie-break the reader cannot see."""
    result = find_teams(db_session, HR, f"Which team knows {NAME} Widget?", "work")
    scores = [t.match_pct for t in result.teams]
    assert len(set(scores)) > 1, f"every unit scored the same: {scores}"


def test_a_team_holding_none_of_the_skills_is_not_a_candidate(fx, db_session):
    result = find_teams(db_session, HR, f"Which team knows {NAME} Other?", "work")
    assert result.teams == []


def test_the_explanation_matches_the_counts_it_was_built_from(fx, db_session):
    result = find_teams(db_session, HR, f"Which team knows {NAME} Widget?", "work")
    weak = _team(result, f"{NAME} Weak Team")
    # Weak Team is Learning-only, so the sentence must not claim expertise.
    assert "Expert" not in weak.why
    strong = _team(result, f"{NAME} Strong Team")
    assert "Expert" in strong.why


def test_skill_names_keep_their_capitalisation(fx, db_session):
    """str.capitalize() lowercases the rest of the string, which turned
    "Expert-level Kubernetes" into "Expert-level kubernetes"."""
    result = find_teams(db_session, HR, f"Which team knows {NAME} Widget?", "work")
    strong = _team(result, f"{NAME} Strong Team")
    assert f"{NAME} Widget" in strong.why


# ---------------------------------------------------------------------------
# The unit head
# ---------------------------------------------------------------------------

def test_the_unit_head_is_whoever_reports_outside_the_unit(fx, db_session):
    wf = load_workforce(db_session, HR, "work")
    head = unit_head(wf, wf.by_unit[fx.strong.id])
    assert head.id == fx.lead.id


def test_a_unit_with_no_visible_head_still_gets_a_contact(fx, db_session):
    """Falls back to the most-reported-to visible member rather than
    returning nobody, so a recommendation is still actionable."""
    wf = load_workforce(db_session, EMPLOYEE, "employee")
    head = unit_head(wf, wf.by_unit[fx.weak.id])
    assert head is not None


# ---------------------------------------------------------------------------
# Reading the question
# ---------------------------------------------------------------------------

def test_model_skills_are_validated_against_the_real_table(fx, db_session):
    need = _need_from_args(db_session, "q", {
        "skills": [f"{NAME} Widget", "Entirely Made Up Skill"], "topic": "widgets"})
    assert need.skills == (f"{NAME} Widget",)
    assert "Entirely Made Up Skill" in need.unrecognised


@pytest.mark.parametrize("args", [{}, {"skills": None}, {"skills": []},
                                  {"skills": "not a list"}, {"skills": [""]}])
def test_malformed_model_output_never_raises(fx, db_session, args):
    need = _need_from_args(db_session, "q", args)
    assert need is None or need.skills == ()


@pytest.mark.parametrize("query", ["", "   ", "asdfghjkl", "hello there", "?????"])
def test_a_question_naming_no_capability_returns_no_teams(fx, db_session, query):
    result = find_teams(db_session, HR, query or "x", "work")
    assert result.teams == []


def test_an_unanswerable_question_is_not_an_error(fx, db_session):
    result = find_teams(db_session, HR, "asdfghjkl", "work")
    assert result.teams == []
    assert result.skills == []


def test_a_literally_named_skill_is_read_without_the_model(fx, db_session):
    need = read_need(db_session, f"Which team is strongest at {NAME} Widget?")
    assert need.skills == (f"{NAME} Widget",)
    assert need.source == "derived"


def test_the_need_type_has_no_field_a_unit_could_land_in():
    """Same absence as TeamPlan: the model describes a capability and has
    nowhere to name a team, a person or a scope."""
    from app.team_finder import Need
    forbidden = {"unit", "org_unit", "org_unit_id", "team", "department",
                 "employee", "employee_ids", "scope", "view_mode"}
    assert not (set(Need.__dataclass_fields__) & forbidden)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

async def test_route_answers_an_ordinary_employee(client, fx):
    """No 403 here, unlike /team/build -- see
    test_an_ordinary_employee_can_search_the_whole_organization."""
    resp = await client.post("/team/find", headers=auth_headers("employee", fx.w2.id),
                             json={"query": f"Which team knows {NAME} Widget?"})
    assert resp.status_code == 200
    assert resp.json()["teams"]


async def test_route_does_not_count_restricted_people(client, fx):
    resp = await client.post("/team/find", headers=auth_headers("employee", fx.w2.id),
                             json={"query": f"Which team knows {NAME} Widget?"})
    body = resp.json()
    strong = next(t for t in body["teams"] if t["name"] == f"{NAME} Strong Team")
    widget = next(s for s in strong["skills"] if s["skill"] == f"{NAME} Widget")
    assert widget["expert"] == 1


async def test_route_rejects_an_empty_query(client, fx):
    resp = await client.post("/team/find", headers=auth_headers("employee", fx.w2.id),
                             json={"query": ""})
    assert resp.status_code == 422


async def test_route_rejects_unknown_fields(client, fx):
    resp = await client.post("/team/find", headers=auth_headers("employee", fx.w2.id),
                             json={"query": "widgets", "org_unit_id": 1})
    assert resp.status_code == 422


async def test_route_round_trips_its_schema(client, fx):
    resp = await client.post("/team/find", headers=auth_headers("hr"),
                             json={"query": f"Which team knows {NAME} Widget?"})
    assert resp.status_code == 200
    assert TeamRecommendationResult.model_validate(resp.json()) is not None


def test_request_schema_rejects_an_overlong_query():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        TeamFindRequest(query="x" * 501)
