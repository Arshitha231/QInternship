"""Tests for app/project_requirements.py — requirement notes CRUD, the
project picker, and the PRD assistant's own read (get_project_requirements_by_name).

Fixture data is created and torn down per test function, isolated by a
distinctive id/name prefix, same pattern as tests/test_project_skills.py.
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from app.auth import AuthenticatedUser
from app.models import (
    Employee, Office, OrgUnit, Project, ProjectRequirementNote, ProjectSkillRequirement, Skill,
)
from app.models.enums import AvailabilityStatus, EmploymentType, ProjectClassification, ProjectType, SkillCategory
from app.project_requirements import (
    RequirementNotesNotAccessible,
    add_requirement_notes,
    get_project_requirements_by_name,
    get_requirement_notes,
    list_projects_for_picker,
)
from app.project_skills import set_required_skills
from app.schemas import AmbiguousProjectMatch, ProjectRequirementsOut, ProjectSkillRequirementIn, RequirementNoteIn
from tests.conftest import auth_headers

PREFIX = "projreq-fixture-"

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
        name="Project Requirements Fixture Engagement", type=ProjectType.project, description="A test engagement.",
        owning_unit_id=org_unit.id, owner_id=owner.id, classification=ProjectClassification.internal,
        is_client_engagement=True,
    )
    confidential = Project(
        name="Project Requirements Fixture Confidential", type=ProjectType.project, description=None,
        owning_unit_id=org_unit.id, owner_id=owner.id, classification=ProjectClassification.confidential,
        is_client_engagement=False,
    )
    bare = Project(
        name="Project Requirements Fixture Bare", type=ProjectType.project, description=None,
        owning_unit_id=org_unit.id, owner_id=owner.id, classification=ProjectClassification.internal,
        is_client_engagement=False,
    )
    db.add_all([project, confidential, bare])
    db.flush()

    skill_a = Skill(name="Project Requirements Fixture Skill A", category=SkillCategory.technical, canonical_id=None)
    db.add(skill_a)
    db.commit()

    yield SimpleNamespace(
        owner=owner, other=other, project=project, confidential=confidential, bare=bare, skill_a=skill_a)

    db.query(ProjectRequirementNote).filter(
        ProjectRequirementNote.project_id.in_([project.id, confidential.id, bare.id])
    ).delete(synchronize_session=False)
    db.query(ProjectSkillRequirement).filter(
        ProjectSkillRequirement.project_id.in_([project.id, confidential.id, bare.id])
    ).delete(synchronize_session=False)
    db.query(Project).filter(Project.name.like("Project Requirements Fixture%")).delete(synchronize_session=False)
    db.query(Skill).filter(Skill.name.like("Project Requirements Fixture%")).delete(synchronize_session=False)
    db.query(Employee).filter(Employee.id.like(f"{PREFIX}%")).delete(synchronize_session=False)
    db.commit()


# --- Requirement notes: read/write access, same shape as required-skills ---

def test_owner_can_add_and_read_requirement_notes(fx, db_session):
    caller = AuthenticatedUser(id=fx.owner.id, role="employee", name=fx.owner.full_name)
    result = add_requirement_notes(db_session, caller, fx.project.id, [
        RequirementNoteIn(note="Client is sensitive about timeline slippage."),
    ])
    assert result is not None
    assert [n.note for n in result] == ["Client is sensitive about timeline slippage."]
    assert get_requirement_notes(db_session, caller, fx.project.id) == result


def test_hr_can_add_requirement_notes_for_any_project(fx, db_session):
    result = add_requirement_notes(db_session, HR, fx.project.id, [RequirementNoteIn(note="Prefers on-site.")])
    assert result is not None


def test_non_owner_employee_cannot_add_requirement_notes(fx, db_session):
    caller = AuthenticatedUser(id=fx.other.id, role="employee", name=fx.other.full_name)
    with pytest.raises(RequirementNotesNotAccessible):
        add_requirement_notes(db_session, caller, fx.project.id, [RequirementNoteIn(note="x")])


def test_non_owner_employee_cannot_read_requirement_notes(fx, db_session):
    # The asymmetry with required-skills' own read route: notes are
    # sentences lifted verbatim from a planning document, gated the same
    # as the write path rather than open to anyone who can see the project.
    add_requirement_notes(db_session, HR, fx.project.id, [RequirementNoteIn(note="x")])
    caller = AuthenticatedUser(id=fx.other.id, role="employee", name=fx.other.full_name)
    with pytest.raises(RequirementNotesNotAccessible):
        get_requirement_notes(db_session, caller, fx.project.id)


def test_adding_requirement_notes_appends_not_replaces(fx, db_session):
    add_requirement_notes(db_session, HR, fx.project.id, [RequirementNoteIn(note="First note.")])
    result = add_requirement_notes(db_session, HR, fx.project.id, [RequirementNoteIn(note="Second note.")])
    assert sorted(n.note for n in result) == ["First note.", "Second note."]


def test_unknown_project_returns_none_for_notes_get(db_session):
    assert get_requirement_notes(db_session, HR, 9_999_999) is None


def test_unknown_project_returns_none_for_notes_add(db_session):
    assert add_requirement_notes(db_session, HR, 9_999_999, [RequirementNoteIn(note="x")]) is None


def test_confidential_project_notes_hidden_from_non_member_non_hr(fx, db_session):
    caller = AuthenticatedUser(id=fx.other.id, role="employee", name=fx.other.full_name)
    assert get_requirement_notes(db_session, caller, fx.confidential.id) is None


def test_confidential_project_notes_visible_to_hr(fx, db_session):
    add_requirement_notes(db_session, HR, fx.confidential.id, [RequirementNoteIn(note="x")])
    assert get_requirement_notes(db_session, HR, fx.confidential.id) is not None


# --- list_projects_for_picker -----------------------------------------------

def test_picker_flags_projects_with_skill_requirements(fx, db_session):
    set_required_skills(db_session, HR, fx.project.id, [ProjectSkillRequirementIn(skill=fx.skill_a.name)])
    items = {p.id: p for p in list_projects_for_picker(db_session, HR)}
    assert items[fx.project.id].has_requirements is True
    assert items[fx.bare.id].has_requirements is False


def test_picker_flags_projects_with_notes_only(fx, db_session):
    add_requirement_notes(db_session, HR, fx.project.id, [RequirementNoteIn(note="x")])
    items = {p.id: p for p in list_projects_for_picker(db_session, HR)}
    assert items[fx.project.id].has_requirements is True


# --- get_project_requirements_by_name (the PRD assistant's own tool) -------

def test_hr_only_fails_fast_before_any_query(fx, db_session):
    caller = AuthenticatedUser(id=fx.owner.id, role="employee", name=fx.owner.full_name)
    # Even the project's own owner -- unlike required-skills' owner-or-hr
    # write gate, this tool is hard HR-only, per this feature's own scope.
    assert get_project_requirements_by_name(db_session, caller, fx.project.name) is None


def test_hr_gets_combined_skills_and_notes_for_a_resolved_project(fx, db_session):
    set_required_skills(db_session, HR, fx.project.id, [ProjectSkillRequirementIn(skill=fx.skill_a.name, minimum_level="Expert")])
    add_requirement_notes(db_session, HR, fx.project.id, [RequirementNoteIn(note="Client is picky about timelines.")])
    result = get_project_requirements_by_name(db_session, HR, fx.project.name)
    assert isinstance(result, ProjectRequirementsOut)
    assert result.project_name == fx.project.name
    assert [(s.skill, s.minimum_level) for s in result.skills] == [(fx.skill_a.name, "Expert")]
    assert [n.note for n in result.notes] == ["Client is picky about timelines."]


def test_no_matching_project_returns_none(db_session):
    assert get_project_requirements_by_name(db_session, HR, "Zzyzx Nonexistent Engagement") is None


def test_ambiguous_project_name_returns_ambiguous_match(fx, db_session):
    # "Fixture" alone matches all three fixture projects by substring.
    result = get_project_requirements_by_name(db_session, HR, "Project Requirements Fixture")
    assert isinstance(result, AmbiguousProjectMatch)
    assert fx.project.name in result.matches


# --- HTTP-level ---------------------------------------------------------

async def test_http_non_hr_non_owner_gets_403_from_get_requirement_notes(client, fx, db_session):
    # The specific case this feature's plan called out explicitly.
    resp = await client.get(
        f"/projects/{fx.project.id}/requirement-notes", headers=auth_headers("employee", fx.other.id))
    assert resp.status_code == 403


async def test_http_owner_can_add_and_read_requirement_notes(client, fx, db_session):
    resp = await client.post(
        f"/projects/{fx.project.id}/requirement-notes",
        json=[{"note": "Client wants weekly updates."}],
        headers=auth_headers("employee", fx.owner.id),
    )
    assert resp.status_code == 200
    assert resp.json() == [{"note": "Client wants weekly updates.", "source_doc_id": None}]

    resp = await client.get(
        f"/projects/{fx.project.id}/requirement-notes", headers=auth_headers("employee", fx.owner.id))
    assert resp.status_code == 200
    assert resp.json() == [{"note": "Client wants weekly updates.", "source_doc_id": None}]


async def test_http_unknown_project_404s_for_notes(client):
    resp = await client.get("/projects/9999999/requirement-notes", headers=auth_headers("hr"))
    assert resp.status_code == 404


async def test_http_list_projects_hr_only(client, fx, db_session):
    resp = await client.get("/projects", headers=auth_headers("hr"))
    assert resp.status_code == 200
    ids = {p["id"] for p in resp.json()}
    assert fx.project.id in ids


async def test_http_list_projects_forbidden_for_non_hr(client):
    resp = await client.get("/projects", headers=auth_headers("employee"))
    assert resp.status_code == 403
