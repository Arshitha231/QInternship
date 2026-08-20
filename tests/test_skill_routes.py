"""Tests for app/skill_routes.py — the shortest introduction chain to a skill.

The things worth pinning down are the ones a reader would take on trust:
that a route is genuinely the SHORTEST one, that every edge it traverses is
something the caller could already see for themselves, and that the two
answers which are not routes — "you already have it" and "nobody you can
reach has it" — come back as themselves rather than as an empty list.

Fixture data is created and torn down per test, isolated by a distinctive
id/name prefix, same pattern as tests/test_analytics.py.
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from app.auth import AuthenticatedUser
from app.models import (
    Employee, EmployeeProject, EmployeeSkill, Office, OrgUnit, Project, Skill,
)
from app.models.enums import (
    AvailabilityStatus, EmploymentType, ProjectClassification, ProjectType,
    SkillCategory, SkillLevel, SkillSource,
)
from app.skill_routes import RouteDenied, find_routes, suggest_skills
from tests.conftest import auth_headers

PREFIX = "route-fixture-"
NAME = "Route Fixture"


def _emp(db, key, org_unit_id, office_id, **over):
    fields = dict(
        id=f"{PREFIX}{key}", directory_object_id=None, full_name=f"{NAME} {key}",
        preferred_name=None, job_title="Consultant", org_unit_id=org_unit_id,
        office_id=office_id, manager_id=None, work_email=f"{PREFIX}{key}@example.test",
        work_phone=None, slack_handle=None, timezone=None, employment_type=EmploymentType.fte,
        hire_date=date(2021, 1, 1), cost_centre=None, personal_mobile=None,
        availability_status=AvailabilityStatus.available, away_until=None, delegate_id=None,
        bio=None, photo_url=None, is_active=True,
    )
    fields.update(over)
    e = Employee(**fields)
    db.add(e)
    return e


@pytest.fixture
def fx(db_session):
    """A deliberately shaped chain:

        me --(project P1)--> bridge --(rare skill S)--> target   [Expert in GOAL]

    plus `far`, who is Expert in GOAL but connected to nobody.

    Every person sits in their OWN org unit. That is not incidental: same-team
    is a bridge, so parking two of them together would connect them directly
    and the chain under test would never be the shortest path. Isolating them
    leaves exactly one way through, which is what makes the assertions below
    about the route rather than about the fixture.
    """
    db = db_session
    office = db.query(Office).first()
    unit = OrgUnit(name=f"{NAME} Unit", parent_id=None, unit_type="team")
    other = OrgUnit(name=f"{NAME} Other Unit", parent_id=None, unit_type="team")
    third = OrgUnit(name=f"{NAME} Third Unit", parent_id=None, unit_type="team")
    fourth = OrgUnit(name=f"{NAME} Fourth Unit", parent_id=None, unit_type="team")
    db.add_all([unit, other, third, fourth])
    db.flush()

    me = _emp(db, "me", unit.id, office.id)
    bridge = _emp(db, "bridge", other.id, office.id)
    target = _emp(db, "target", third.id, office.id)
    far = _emp(db, "far", fourth.id, office.id)
    db.flush()

    goal = Skill(name=f"{NAME} Goal Skill", category=SkillCategory.technical, canonical_id=None)
    rare = Skill(name=f"{NAME} Rare Skill", category=SkillCategory.technical, canonical_id=None)
    common = Skill(name=f"{NAME} Common Language", category=SkillCategory.language, canonical_id=None)
    db.add_all([goal, rare, common])
    db.flush()

    def skill(emp, sk, level):
        db.add(EmployeeSkill(employee_id=emp.id, skill_id=sk.id, level=level,
                             source=SkillSource.confirmed, verified_at=None))

    # bridge and target share the rare skill -> an edge.
    skill(bridge, rare, SkillLevel.working)
    skill(target, rare, SkillLevel.expert)
    # target and far can both do the goal; only target is reachable.
    skill(target, goal, SkillLevel.expert)
    skill(far, goal, SkillLevel.expert)
    # A language everybody has must never become an edge.
    for e in (me, bridge, target, far):
        skill(e, common, SkillLevel.working)

    project = Project(
        name=f"{NAME} Shared Project", type=ProjectType.project, description="",
        owning_unit_id=unit.id, owner_id=me.id, classification=ProjectClassification.internal,
        is_client_engagement=False,
    )
    db.add(project)
    db.flush()
    for e in (me, bridge):
        db.add(EmployeeProject(employee_id=e.id, project_id=project.id, role="Engineer",
                               contribution=None, start_date=date(2024, 1, 1), end_date=None))
    db.commit()

    caller = AuthenticatedUser(id=me.id, role="employee", name=me.full_name)
    yield SimpleNamespace(me=me, bridge=bridge, target=target, far=far, unit=unit, other=other,
                          third=third, fourth=fourth, goal=goal, rare=rare, common=common,
                          project=project, caller=caller)

    ids = [me.id, bridge.id, target.id, far.id]
    db.query(EmployeeProject).filter(EmployeeProject.employee_id.in_(ids)).delete(synchronize_session=False)
    db.query(Project).filter_by(id=project.id).delete(synchronize_session=False)
    db.query(EmployeeSkill).filter(EmployeeSkill.employee_id.in_(ids)).delete(synchronize_session=False)
    db.query(Skill).filter(Skill.name.like(f"{NAME}%")).delete(synchronize_session=False)
    db.query(Employee).filter(Employee.id.like(f"{PREFIX}%")).delete(synchronize_session=False)
    db.query(OrgUnit).filter(OrgUnit.name.like(f"{NAME}%")).delete(synchronize_session=False)
    db.commit()


# --- The route itself -------------------------------------------------------

def test_finds_the_chain_and_labels_every_step(fx, db_session):
    result = find_routes(db_session, fx.caller, fx.me.id, fx.goal.name, "employee")
    route = next(r for r in result.routes if r.target.id == fx.target.id)
    assert [h.person.id for h in route.hops] == [fx.bridge.id, fx.target.id]
    # The labels are the feature: they are the opening line of the message.
    assert (route.hops[0].via_kind, route.hops[0].via) == ("project", fx.project.name)
    assert (route.hops[1].via_kind, route.hops[1].via) == ("skill", fx.rare.name)
    assert route.level == "Expert"


def test_the_route_is_the_shortest_one(fx, db_session):
    """Giving `me` the rare skill too creates a direct edge to target, and
    the answer must collapse to one hop rather than keep the old two."""
    db_session.add(EmployeeSkill(employee_id=fx.me.id, skill_id=fx.rare.id,
                                 level=SkillLevel.working, source=SkillSource.confirmed,
                                 verified_at=None))
    db_session.commit()
    result = find_routes(db_session, fx.caller, fx.me.id, fx.goal.name, "employee")
    route = next(r for r in result.routes if r.target.id == fx.target.id)
    assert len(route.hops) == 1
    assert route.hops[0].person.id == fx.target.id


def test_a_shared_language_is_never_a_bridge(fx, db_session):
    """All four people share the language. If it were an edge, everybody
    would be one hop from everybody and the shared-language step would show
    up in a route -- asserted against the rule directly rather than against
    who is reachable, because the goal skill legitimately bridges its own
    holders to each other (see test_the_searched_skill_bridges_its_holders)."""
    result = find_routes(db_session, fx.caller, fx.me.id, fx.goal.name, "employee")
    vias = {(h.via_kind, h.via) for r in result.routes for h in r.hops}
    assert ("skill", fx.common.name) not in vias
    # And nobody is reachable in one hop except the person sharing the project.
    one_hop = {r.hops[0].person.id for r in result.routes}
    assert one_hop <= {fx.bridge.id}


def test_teammates_bridge(fx, db_session):
    """Same org unit is a real introduction. Moving `me` in beside target
    should produce a one-hop route that says so."""
    fx.me.org_unit_id = fx.third.id
    db_session.commit()
    result = find_routes(db_session, fx.caller, fx.me.id, fx.goal.name, "employee")
    route = next(r for r in result.routes if r.target.id == fx.target.id)
    assert len(route.hops) == 1
    assert route.hops[0].via_kind == "team"


def test_a_common_skill_is_not_distinctive_enough_to_bridge(fx, db_session, monkeypatch):
    """The distinctiveness cap is what stops "we both know SQL" counting as
    an introduction."""
    monkeypatch.setattr("app.skill_routes.SKILL_BRIDGE_MAX_HOLDERS", 1)
    result = find_routes(db_session, fx.caller, fx.me.id, fx.goal.name, "employee")
    # bridge->target ran on the rare skill, which now has 2 holders > cap.
    assert all(r.target.id != fx.target.id for r in result.routes)


def test_hop_limit_is_respected(fx, db_session):
    result = find_routes(db_session, fx.caller, fx.me.id, fx.goal.name, "employee", max_hops=1)
    assert all(len(r.hops) <= 1 for r in result.routes)


# --- The answers that are not routes ---------------------------------------

def test_already_having_the_skill_is_said_not_routed(fx, db_session):
    db_session.add(EmployeeSkill(employee_id=fx.me.id, skill_id=fx.goal.id,
                                 level=SkillLevel.working, source=SkillSource.confirmed,
                                 verified_at=None))
    db_session.commit()
    result = find_routes(db_session, fx.caller, fx.me.id, fx.goal.name, "employee")
    assert result.already_capable is True
    assert result.routes == []


def test_the_searched_skill_bridges_its_holders(fx, db_session):
    """`far` shares the goal skill with `target`, and the goal skill is
    distinctive (2 holders), so it is a bridge like any other -- reaching
    `far` one hop past `target` is correct, not a leak. Pinned because it is
    the surprising half of the previous test."""
    result = find_routes(db_session, fx.caller, fx.me.id, fx.goal.name, "employee")
    far = next((r for r in result.routes if r.target.id == fx.far.id), None)
    assert far is not None
    assert [h.person.id for h in far.hops] == [fx.bridge.id, fx.target.id, fx.far.id]


def test_unreachable_holders_are_counted_not_hidden(fx, db_session):
    """"Nobody you can reach has it" and "nobody has it" are different
    answers, and an empty list alone would read as the second. Bounded to two
    hops so `far` (three away) is genuinely out of range."""
    result = find_routes(db_session, fx.caller, fx.me.id, fx.goal.name, "employee", max_hops=2)
    assert result.skill.capable_count == 2          # target and far
    assert {r.target.id for r in result.routes} == {fx.target.id}
    assert result.unreachable_holder_count == 1     # far


def test_an_unknown_skill_reports_itself_as_unresolved(fx, db_session):
    result = find_routes(db_session, fx.caller, fx.me.id, "Nonexistent Skill Zzz", "employee")
    assert result.skill is None
    assert result.routes == []


def test_a_learning_level_holder_is_not_a_destination(fx, db_session):
    """Capability, not familiarity: you do not get introduced to somebody
    who is still learning it."""
    db_session.query(EmployeeSkill).filter_by(
        employee_id=fx.target.id, skill_id=fx.goal.id).delete(synchronize_session=False)
    db_session.add(EmployeeSkill(employee_id=fx.target.id, skill_id=fx.goal.id,
                                 level=SkillLevel.learning, source=SkillSource.confirmed,
                                 verified_at=None))
    db_session.commit()
    result = find_routes(db_session, fx.caller, fx.me.id, fx.goal.name, "employee")
    assert all(r.target.id != fx.target.id for r in result.routes)


# --- Who may ask ------------------------------------------------------------

def test_you_may_ask_about_yourself(fx, db_session):
    assert find_routes(db_session, fx.caller, fx.me.id, fx.goal.name, "employee") is not None


def test_you_may_not_ask_about_somebody_else(fx, db_session):
    with pytest.raises(RouteDenied):
        find_routes(db_session, fx.caller, fx.bridge.id, fx.goal.name, "employee")


def test_hr_may_ask_on_anyones_behalf(fx, db_session):
    hr = AuthenticatedUser(id=f"{PREFIX}hr", role="hr", name="HR")
    assert find_routes(db_session, hr, fx.bridge.id, fx.goal.name, "work") is not None


def test_hr_in_employee_mode_loses_that(fx, db_session):
    hr = AuthenticatedUser(id=f"{PREFIX}hr", role="hr", name="HR")
    with pytest.raises(RouteDenied):
        find_routes(db_session, hr, fx.bridge.id, fx.goal.name, "employee")


def test_a_restricted_person_is_not_walked_through(fx, db_session):
    """A route stepping through somebody the caller cannot see would
    disclose their existence, which is what the restriction is for."""
    fx.bridge.availability_status = AvailabilityStatus.restricted
    db_session.commit()
    result = find_routes(db_session, fx.caller, fx.me.id, fx.goal.name, "employee")
    assert all(fx.bridge.id not in {h.person.id for h in r.hops} for r in result.routes)


# --- Suggestions ------------------------------------------------------------

def test_suggestions_carry_their_reason(fx, db_session):
    out = suggest_skills(db_session, fx.caller, fx.me.id, "employee")
    assert out, "expected at least one suggestion"
    assert all(s.reason for s in out)


def test_suggestions_never_include_something_you_have(fx, db_session):
    out = suggest_skills(db_session, fx.caller, fx.me.id, "employee")
    assert all(s.skill != fx.common.name for s in out)


# --- HTTP -------------------------------------------------------------------

async def test_route_endpoint_refuses_somebody_elses_profile(client, fx):
    resp = await client.get(
        f"/people/{fx.bridge.id}/skill-routes?skill={fx.goal.name}",
        headers=auth_headers("employee", fx.me.id),
    )
    assert resp.status_code == 403


async def test_route_endpoint_returns_the_chain(client, fx):
    resp = await client.get(
        f"/people/{fx.me.id}/skill-routes?skill={fx.goal.name}",
        headers=auth_headers("employee", fx.me.id),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["skill"]["skill"] == fx.goal.name
    assert any(len(r["hops"]) == 2 for r in body["routes"])
