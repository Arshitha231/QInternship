"""Tests for app/project_skills.py — who can read/write a project's
required skills, and how skill names/levels get resolved and replaced.

Fixture data is created and torn down per test function, isolated by a
distinctive id/name prefix, same pattern as tests/test_continuity.py.
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from app.auth import AuthenticatedUser
from app.models import Employee, Office, OrgUnit, Project, ProjectSkillRequirement, Skill
from app.models.enums import AvailabilityStatus, EmploymentType, ProjectClassification, ProjectType, SkillCategory
from app.project_skills import ProjectNotWritable, UnknownSkill, get_required_skills, set_required_skills
from app.schemas import ProjectSkillRequirementIn
from tests.conftest import auth_headers

PREFIX = "projskill-fixture-"

HR = AuthenticatedUser(id=f"{PREFIX}hr-caller", role="hr", name="Test HR")


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


@pytest.fixture
def fx(db_session):
    db = db_session
    org_unit = db.query(OrgUnit).filter_by(name="Platform Engineering").first()
    office = db.query(Office).first()

    owner = _mkemp(db, "owner", "Fixture Owner", org_unit.id, office.id)
    other = _mkemp(db, "other", "Fixture Other", org_unit.id, office.id)
    db.flush()

    project = Project(
        name="Project Skills Fixture Engagement", type=ProjectType.project, description="",
        owning_unit_id=org_unit.id, owner_id=owner.id, classification=ProjectClassification.internal,
        is_client_engagement=True,
    )
    confidential = Project(
        name="Project Skills Fixture Confidential", type=ProjectType.project, description="",
        owning_unit_id=org_unit.id, owner_id=owner.id, classification=ProjectClassification.confidential,
        is_client_engagement=False,
    )
    db.add_all([project, confidential])
    db.flush()

    skill_a = Skill(name="Project Skills Fixture Skill A", category=SkillCategory.technical, canonical_id=None)
    skill_synonym = Skill(name="Project Skills Fixture Synonym", category=SkillCategory.technical, canonical_id=None)
    db.add_all([skill_a, skill_synonym])
    db.flush()
    skill_synonym.canonical_id = skill_a.id  # alias, to exercise resolve_skill's synonym path
    db.commit()

    yield SimpleNamespace(owner=owner, other=other, project=project, confidential=confidential,
                          skill_a=skill_a, skill_synonym=skill_synonym)

    db.query(ProjectSkillRequirement).filter(
        ProjectSkillRequirement.project_id.in_([project.id, confidential.id])
    ).delete(synchronize_session=False)
    db.query(Project).filter(Project.name.like("Project Skills Fixture%")).delete(synchronize_session=False)
    db.query(Skill).filter(Skill.name.like("Project Skills Fixture%")).delete(synchronize_session=False)
    db.query(Employee).filter(Employee.id.like(f"{PREFIX}%")).delete(synchronize_session=False)
    db.commit()


# --- Write access: owner or hr only -----------------------------------------

def test_owner_can_set_required_skills(fx, db_session):
    caller = AuthenticatedUser(id=fx.owner.id, role="employee", name=fx.owner.full_name)
    result = set_required_skills(db_session, caller, fx.project.id, [
        ProjectSkillRequirementIn(skill=fx.skill_a.name, minimum_level="Expert"),
    ])
    assert result is not None
    assert [(r.skill, r.minimum_level) for r in result] == [(fx.skill_a.name, "Expert")]


def test_hr_can_set_required_skills_for_any_project(fx, db_session):
    result = set_required_skills(db_session, HR, fx.project.id, [
        ProjectSkillRequirementIn(skill=fx.skill_a.name),
    ])
    assert result is not None


def test_non_owner_employee_cannot_set_required_skills(fx, db_session):
    caller = AuthenticatedUser(id=fx.other.id, role="employee", name=fx.other.full_name)
    with pytest.raises(ProjectNotWritable):
        set_required_skills(db_session, caller, fx.project.id, [])


def test_manager_who_is_not_owner_cannot_set_required_skills(fx, db_session):
    caller = AuthenticatedUser(id=fx.other.id, role="manager", name=fx.other.full_name)
    with pytest.raises(ProjectNotWritable):
        set_required_skills(db_session, caller, fx.project.id, [])


# --- Skill resolution and replace semantics ---------------------------------

def test_unknown_skill_name_raises_and_writes_nothing(fx, db_session):
    caller = AuthenticatedUser(id=fx.owner.id, role="employee", name=fx.owner.full_name)
    with pytest.raises(UnknownSkill):
        set_required_skills(db_session, caller, fx.project.id, [
            ProjectSkillRequirementIn(skill="Not A Real Skill Name At All"),
        ])
    assert get_required_skills(db_session, caller, fx.project.id) == []


def test_synonym_resolves_to_canonical_skill_name(fx, db_session):
    caller = AuthenticatedUser(id=fx.owner.id, role="employee", name=fx.owner.full_name)
    result = set_required_skills(db_session, caller, fx.project.id, [
        ProjectSkillRequirementIn(skill=fx.skill_synonym.name, minimum_level="Expert"),
    ])
    assert [(r.skill, r.minimum_level) for r in result] == [(fx.skill_a.name, "Expert")]


def test_setting_required_skills_replaces_the_full_set(fx, db_session):
    caller = AuthenticatedUser(id=fx.owner.id, role="employee", name=fx.owner.full_name)
    set_required_skills(db_session, caller, fx.project.id, [
        ProjectSkillRequirementIn(skill=fx.skill_a.name),
    ])
    result = set_required_skills(db_session, caller, fx.project.id, [
        ProjectSkillRequirementIn(skill=fx.skill_synonym.name, minimum_level="Expert"),
    ])
    # Second call fully replaces the first -- not an incremental add.
    assert [(r.skill, r.minimum_level) for r in result] == [(fx.skill_a.name, "Expert")]


def test_duplicate_skill_names_in_one_request_last_one_wins(fx, db_session):
    caller = AuthenticatedUser(id=fx.owner.id, role="employee", name=fx.owner.full_name)
    result = set_required_skills(db_session, caller, fx.project.id, [
        ProjectSkillRequirementIn(skill=fx.skill_a.name, minimum_level="Learning"),
        ProjectSkillRequirementIn(skill=fx.skill_a.name, minimum_level="Expert"),
    ])
    assert [(r.skill, r.minimum_level) for r in result] == [(fx.skill_a.name, "Expert")]


# --- Visibility: not sensitive, but confidential projects still gate -------

def test_unknown_project_returns_none_for_get(db_session):
    assert get_required_skills(db_session, HR, 9_999_999) is None


def test_unknown_project_returns_none_for_set(db_session):
    assert set_required_skills(db_session, HR, 9_999_999, []) is None


def test_confidential_project_hidden_from_non_member_non_hr(fx, db_session):
    caller = AuthenticatedUser(id=fx.other.id, role="employee", name=fx.other.full_name)
    assert get_required_skills(db_session, caller, fx.confidential.id) is None


def test_confidential_project_visible_to_hr(fx, db_session):
    set_required_skills(db_session, HR, fx.confidential.id, [ProjectSkillRequirementIn(skill=fx.skill_a.name)])
    assert get_required_skills(db_session, HR, fx.confidential.id) is not None


# --- HTTP-level ---------------------------------------------------------

async def test_http_owner_can_set_required_skills(client, fx, db_session):
    resp = await client.put(
        f"/projects/{fx.project.id}/required-skills",
        json=[{"skill": fx.skill_a.name, "minimum_level": "Working"}],
        headers=auth_headers("employee", fx.owner.id),
    )
    assert resp.status_code == 200
    assert resp.json() == [{"skill": fx.skill_a.name, "minimum_level": "Working"}]


async def test_http_non_owner_gets_403(client, fx, db_session):
    resp = await client.put(
        f"/projects/{fx.project.id}/required-skills",
        json=[{"skill": fx.skill_a.name}],
        headers=auth_headers("employee", fx.other.id),
    )
    assert resp.status_code == 403


async def test_http_unknown_skill_gets_422(client, fx, db_session):
    resp = await client.put(
        f"/projects/{fx.project.id}/required-skills",
        json=[{"skill": "Not A Real Skill"}],
        headers=auth_headers("employee", fx.owner.id),
    )
    assert resp.status_code == 422


async def test_http_get_required_skills_unknown_project_404s(client):
    resp = await client.get("/projects/9999999/required-skills", headers=auth_headers("employee"))
    assert resp.status_code == 404


async def test_http_get_required_skills_open_to_any_authenticated_role(client, fx, db_session):
    resp = await client.get(f"/projects/{fx.project.id}/required-skills", headers=auth_headers("employee"))
    assert resp.status_code == 200
