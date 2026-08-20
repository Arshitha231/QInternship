"""Tests for app/project_requirements.py — requirement notes CRUD, the
project picker, and the PRD assistant's own read (get_project_requirements_by_name).

Fixture data is created and torn down per test function, isolated by a
distinctive id/name prefix, same pattern as tests/test_project_skills.py.
"""
from __future__ import annotations

import io
from datetime import date
from types import SimpleNamespace

import pytest

from app.auth import AuthenticatedUser
from app.models import (
    Employee, Office, OrgUnit, Project, ProjectRequirementNote, ProjectSkillRequirement, Skill, UploadedDoc,
)
from app.models.enums import AvailabilityStatus, EmploymentType, ProjectClassification, ProjectType, SkillCategory
from app.project_requirements import (
    RequirementNotesNotAccessible,
    add_requirement_notes,
    get_project_requirements_by_name,
    get_requirement_notes,
    list_project_requirements_summary,
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


# --- list_project_requirements_summary --------------------------------------

def test_summary_lists_only_projects_with_requirements(fx, db_session):
    set_required_skills(db_session, HR, fx.project.id, [ProjectSkillRequirementIn(skill=fx.skill_a.name)])
    summary = {row.project_name: row for row in list_project_requirements_summary(db_session, HR)}
    assert fx.project.name in summary
    assert fx.bare.name not in summary


def test_summary_counts_skills_and_notes_separately(fx, db_session):
    set_required_skills(db_session, HR, fx.project.id, [ProjectSkillRequirementIn(skill=fx.skill_a.name)])
    add_requirement_notes(db_session, HR, fx.project.id, [
        RequirementNoteIn(note="a"), RequirementNoteIn(note="b"),
    ])
    summary = {row.project_name: row for row in list_project_requirements_summary(db_session, HR)}
    assert summary[fx.project.name].skill_count == 1
    assert summary[fx.project.name].note_count == 2


def test_summary_is_hard_hr_only(fx, db_session):
    set_required_skills(db_session, HR, fx.project.id, [ProjectSkillRequirementIn(skill=fx.skill_a.name)])
    caller = AuthenticatedUser(id=fx.owner.id, role="employee", name=fx.owner.full_name)
    # Even the project's OWNER -- unlike required-skills' owner-or-hr write
    # gate, this tool is hard HR-only, same as get_project_requirements_by_name.
    assert list_project_requirements_summary(db_session, caller) == []


def test_summary_empty_when_nothing_declared(db_session):
    caller = AuthenticatedUser(id="summary-empty-hr", role="hr")
    # Not a strict isolation guarantee against other tests' fixture data
    # (this queries the whole table), but with no requirements declared
    # anywhere the result is at minimum well-formed.
    result = list_project_requirements_summary(db_session, caller)
    assert isinstance(result, list)


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


# --- PRD upload (extraction preview, no writes) + confirm-time scrub -------

def _docx_bytes(text: str) -> bytes:
    import docx

    document = docx.Document()
    for line in text.splitlines():
        document.add_paragraph(line)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


_PRD_TEXT = """Meridian Health -- Claims Platform Modernization

This engagement requires Terraform at Expert level.
The client is sensitive about timeline slippage.
"""


async def _upload_prd(client, project_id, role="hr", user_id="prd-uploader-1", filename="prd.docx"):
    return await client.post(
        f"/projects/{project_id}/prd",
        files={"file": (filename, _docx_bytes(_PRD_TEXT),
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
        headers=auth_headers(role, user_id),
    )


async def test_http_prd_upload_returns_a_preview_without_writing_anything(client, fx, db_session):
    resp = await _upload_prd(client, fx.project.id)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["skills"][0]["skill"] == "Terraform"
    assert body["notes"][0]["note"]
    # A preview, not a write -- neither table gained a row from the upload
    # alone.
    assert get_requirement_notes(db_session, HR, fx.project.id) == []
    assert db_session.query(ProjectSkillRequirement).filter_by(project_id=fx.project.id).count() == 0


async def test_http_prd_upload_splits_an_unrecognized_skill_into_new_skills(client, fx, db_session):
    # Live bug: extraction (mock or real) reads the document, not this
    # system's skill vocabulary, and can propose a name -- "Communication"
    # -- that PUT .../required-skills' own resolve_skill() has never heard
    # of. Left in the "skills" list, confirming the untouched preview 422s
    # whole-batch (UnknownSkill) with no indication of which row caused it.
    # The route must split it into `new_skills` instead, so HR can see and
    # explicitly approve the catalog-creation decision, same as
    # app.proposals._commit_skill already does for the resume pipeline.
    text = "Meridian Health -- Claims Platform Modernization\n\nThis engagement requires Communication.\n"
    resp = await client.post(
        f"/projects/{fx.project.id}/prd",
        files={"file": ("prd.docx", _docx_bytes(text),
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
        headers=auth_headers("hr", "prd-uploader-2"),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert not any(s["skill"] == "Communication" for s in body["skills"])
    assert any(s["skill"] == "Communication" for s in body["new_skills"])
    assert not any("Communication" in n["note"] for n in body["notes"])

    # The `skills` half of the preview is confirmable as-is -- no 422.
    from app.project_skills import set_required_skills
    from app.schemas import ProjectSkillRequirementIn

    confirmable = [ProjectSkillRequirementIn(skill=s["skill"], minimum_level=s["minimum_level"]) for s in body["skills"]]
    result = set_required_skills(db_session, HR, fx.project.id, confirmable)
    assert result is not None


async def test_http_prd_upload_forbidden_for_non_hr(client, fx, db_session):
    resp = await _upload_prd(client, fx.project.id, role="employee", user_id=fx.owner.id)
    assert resp.status_code == 403


async def test_http_prd_upload_unknown_project_404s(client):
    resp = await _upload_prd(client, 9_999_999)
    assert resp.status_code == 404


async def test_confirming_a_note_scrubs_its_source_document(client, fx, db_session):
    upload = await _upload_prd(client, fx.project.id)
    doc_id = upload.json()["doc_id"]

    doc = db_session.get(UploadedDoc, doc_id)
    assert doc.project_id == fx.project.id
    assert doc.extracted_text  # still has the full text before confirm
    assert doc.content_scrubbed_at is None

    result = add_requirement_notes(db_session, HR, fx.project.id, [
        RequirementNoteIn(note="Client is sensitive about timeline slippage.", source_doc_id=doc_id),
    ])
    assert result is not None

    db_session.refresh(doc)
    assert doc.extracted_text == ""
    assert doc.content_scrubbed_at is not None


async def test_confirming_a_note_with_no_source_doc_id_scrubs_nothing(fx, db_session):
    add_requirement_notes(db_session, HR, fx.project.id, [RequirementNoteIn(note="Hand-authored, no document.")])
    # Nothing to assert against by id -- this just confirms the write path
    # above doesn't raise when source_doc_id is absent, the ordinary
    # hand-authored-note case.


async def test_scrub_never_touches_a_docs_upload_row_with_no_project_id(db_session):
    # The existing /docs/upload pipeline's own rows (status reports,
    # resumes) must never be scrubbed by this path -- only PRD uploads
    # (project_id set) are in scope.
    from datetime import datetime

    other_doc = UploadedDoc(
        filename="status.docx", content_type="application/octet-stream", byte_size=10,
        extracted_text="Alex Kim worked on Project X.", uploaded_by=HR.id, uploaded_at=datetime.now(),
        project_id=None,
    )
    db_session.add(other_doc)
    db_session.commit()
    doc_id = other_doc.id

    project = Project(
        name="Project Requirements Fixture Scrub Isolation", type=ProjectType.project, description=None,
        owning_unit_id=db_session.query(OrgUnit).filter_by(name="Platform Engineering").first().id,
        owner_id=HR.id, classification=ProjectClassification.internal, is_client_engagement=False,
    )
    db_session.add(project)
    db_session.commit()

    add_requirement_notes(db_session, HR, project.id, [
        RequirementNoteIn(note="x", source_doc_id=doc_id),
    ])

    db_session.refresh(other_doc)
    assert other_doc.extracted_text == "Alex Kim worked on Project X."
    assert other_doc.content_scrubbed_at is None

    db_session.query(ProjectRequirementNote).filter_by(project_id=project.id).delete()
    db_session.query(Project).filter_by(id=project.id).delete()
    db_session.query(UploadedDoc).filter_by(id=doc_id).delete()
    db_session.commit()


async def test_scrub_never_touches_a_document_from_a_different_project(fx, db_session):
    # IDOR regression: source_doc_id is caller-supplied and otherwise
    # unvalidated, and _owner_or_hr only requires the caller to own THE
    # PROJECT THE NOTE IS FOR -- an ordinary (non-HR) project owner, not
    # just HR. Without the project_id filter on the scrub query, that
    # owner could pass an arbitrary/enumerated source_doc_id belonging to
    # a DIFFERENT project -- including one they have no visibility into at
    # all, like fx.confidential here -- and irreversibly wipe its extracted
    # text. The note write itself succeeding (this caller genuinely owns
    # fx.project) must not let the scrub reach outside that project.
    from datetime import datetime

    other_project_doc = UploadedDoc(
        filename="confidential-prd.docx", content_type="application/octet-stream", byte_size=10,
        extracted_text="Confidential engagement details.", uploaded_by=HR.id, uploaded_at=datetime.now(),
        project_id=fx.confidential.id,
    )
    db_session.add(other_project_doc)
    db_session.commit()
    doc_id = other_project_doc.id

    caller = AuthenticatedUser(id=fx.owner.id, role="employee", name=fx.owner.full_name)
    result = add_requirement_notes(db_session, caller, fx.project.id, [
        RequirementNoteIn(note="x", source_doc_id=doc_id),
    ])
    assert result is not None  # the note itself is legitimate -- caller owns fx.project

    db_session.refresh(other_project_doc)
    assert other_project_doc.extracted_text == "Confidential engagement details."
    assert other_project_doc.content_scrubbed_at is None

    db_session.query(UploadedDoc).filter_by(id=doc_id).delete()
    db_session.commit()
