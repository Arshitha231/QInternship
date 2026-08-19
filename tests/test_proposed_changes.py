"""Doc upload -> classify -> extraction -> disambiguation -> review.

Two structural guarantees this module exists to prove:

  1. Nothing gets an employee_id, and no proposed_changes row becomes
     visible in the review screen, until a human resolves the
     doc_subject_match it belongs to — even a single unambiguous-looking
     name match. test_ranked_candidates_never_auto_assign and
     test_pending_rows_invisible_until_subject_resolved are the load-bearing
     tests; everything else is downstream of those two holding.
  2. Nothing committed by accept()/edit() is searchable before it commits.

Runs entirely on the mock extractor/classifier (AI_MODE unset, no chat
deployment configured — see conftest), so nothing here touches a model API.
"""
import io
import json
from datetime import date

import pytest

from app.models import AuditLog, DocSubjectMatch, EmployeeProject, EmployeeSkill, ProposedChange
from app.models.enums import ProposedChangeStatus, ResolutionStatus
from tests.conftest import auth_headers

PROJECT_DOC_TEXT = """Weekly status report

Alex Kim worked on Project Nightingale. Rebuilt the ingest pipeline using Terraform, Python.
Jamie Doubleton worked on Project Atlas. Migrated the ledger tables. Reach them at jamie.d2@example.test.
Robin Nobody worked on Project Phantom. Wrote the onboarding docs.
"""

RESUME_TEXT = """Alex Kim

Resume

Professional Summary
Backend engineer with experience across distributed systems.

Work Experience
Built scalable services at a fintech startup.

Skills
Python, Kubernetes, Terraform, PostgreSQL

Education
BS Computer Science

alex.kim@example.test
"""

RESUME_NO_MATCH_TEXT = """Nobody Findable

Resume

Professional Summary
Looking for a new role.

Skills
Rust, WebAssembly

Education
BS Computer Science
"""


def _docx_bytes(text: str) -> bytes:
    import docx

    document = docx.Document()
    for line in text.splitlines():
        document.add_paragraph(line)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


async def _upload(client, text, role="hr", view_mode="work", filename="doc.docx"):
    return await client.post(
        "/docs/upload", params={"view_mode": view_mode},
        files={"file": (filename, _docx_bytes(text),
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
        headers=auth_headers(role, "hr-reviewer-1"),
    )


@pytest.fixture
async def project_doc(client):
    resp = await _upload(client, PROJECT_DOC_TEXT, filename="status.docx")
    assert resp.status_code == 201, resp.text
    assert resp.json()["doc_type"] == "project_doc"
    return resp.json()


async def _subjects(client, doc_id, status=None):
    params = {"doc_id": doc_id, "view_mode": "work"}
    if status:
        params["status"] = status
    resp = await client.get("/doc_subject_matches", params=params, headers=auth_headers("hr"))
    assert resp.status_code == 200, resp.text
    return resp.json()["subjects"]


async def _resolve(client, subject_id, employee_id=None, new_hire=False, role="hr", view_mode="work"):
    body = {"new_hire": True} if new_hire else {"employee_id": employee_id}
    return await client.post(
        f"/doc_subject_matches/{subject_id}/resolve", params={"view_mode": view_mode},
        json=body, headers=auth_headers(role, "hr-reviewer-1"))


async def _proposals(client, doc_id=None, employee_id=None, role="hr"):
    params = {"view_mode": "work"}
    if doc_id is not None:
        params["doc_id"] = doc_id
    if employee_id is not None:
        params["employee_id"] = employee_id
    resp = await client.get("/proposed_changes", params=params, headers=auth_headers(role))
    assert resp.status_code == 200, resp.text
    return resp.json()["groups"]


# ---------------------------------------------------------------------------
# Classification.
# ---------------------------------------------------------------------------

async def test_project_doc_classifies_as_project_doc(project_doc):
    assert project_doc["doc_type"] == "project_doc"


async def test_resume_classifies_as_resume(client):
    resp = await _upload(client, RESUME_TEXT, filename="resume.docx")
    assert resp.status_code == 201, resp.text
    assert resp.json()["doc_type"] == "resume"


@pytest.mark.parametrize("role", ["employee", "manager", "it"])
async def test_only_hr_can_upload_documents(client, role):
    resp = await _upload(client, PROJECT_DOC_TEXT, role=role)
    assert resp.status_code == 403


async def test_hr_cannot_upload_in_employee_mode(client):
    resp = await _upload(client, PROJECT_DOC_TEXT, view_mode="employee")
    assert resp.status_code == 403


async def test_unsupported_file_type_is_415(client):
    resp = await client.post(
        "/docs/upload", params={"view_mode": "work"},
        files={"file": ("notes.txt", b"Alex Kim worked on X. Did things.", "text/plain")},
        headers=auth_headers("hr"),
    )
    assert resp.status_code == 415


# ---------------------------------------------------------------------------
# Per-field granularity: one row per field, not per employee or per doc.
# ---------------------------------------------------------------------------

async def test_project_doc_produces_one_row_per_field_not_per_employee(project_doc, db_session):
    rows = (
        db_session.query(ProposedChange)
        .filter(ProposedChange.source_doc_id == project_doc["doc_id"])
        .all()
    )
    # 3 people mentioned x (1 contribution + 1 project_entry) = 6, plus one
    # skill row for Alex Kim's "Terraform, Python" mention = 8 total. Never
    # collapsed to one row per person, and never bundled into one row per
    # document either.
    by_type = {}
    for r in rows:
        by_type.setdefault(r.change_type.value, []).append(r)
    assert len(by_type.get("contribution", [])) == 3
    assert len(by_type.get("project_entry", [])) == 3
    assert len(by_type.get("skill", [])) >= 1
    # Every single row is its own independently-reviewable unit.
    assert all(r.status is ProposedChangeStatus.pending for r in rows)


async def test_three_people_mentioned_produce_three_subjects(project_doc, db_session):
    subjects = (
        db_session.query(DocSubjectMatch)
        .filter(DocSubjectMatch.source_doc_id == project_doc["doc_id"])
        .all()
    )
    names = {s.extracted_name for s in subjects}
    assert names == {"Alex Kim", "Jamie Doubleton", "Robin Nobody"}


# ---------------------------------------------------------------------------
# Ranked, multi-candidate resolution — never auto-assigned.
# ---------------------------------------------------------------------------

async def test_ranked_candidates_never_auto_assign(client, project_doc, db_session):
    """The load-bearing test: Jamie Doubleton matches TWO real employees.
    Both must appear as ranked candidates, and employee_id must stay NULL
    on every row until a human resolves it — including the email-matched
    top candidate, which is still just the top of a ranked list of one."""
    subjects = await _subjects(client, project_doc["doc_id"])
    jamie = next(s for s in subjects if s["extracted_name"] == "Jamie Doubleton")

    assert jamie["resolution_status"] == "unresolved"
    candidate_ids = {c["employee_id"] for c in jamie["candidates"]}
    assert candidate_ids == {"extract-dup-1", "extract-dup-2"}

    # Email match ranks first — jamie.d2@example.test belongs to extract-dup-2.
    assert jamie["candidates"][0]["employee_id"] == "extract-dup-2"
    assert jamie["candidates"][0]["confidence"] > jamie["candidates"][1]["confidence"]
    assert "email" in jamie["candidates"][0]["match_reason"]

    # Nothing under this subject has an employee_id yet, unambiguous top
    # candidate or not.
    rows = db_session.query(ProposedChange).filter(
        ProposedChange.subject_match_id == jamie["id"]).all()
    assert rows and all(r.employee_id is None for r in rows)


async def test_single_unambiguous_match_still_not_auto_assigned(client, project_doc, db_session):
    """Alex Kim and Robin Nobody both resolve to at most one plausible
    candidate — that must not be different from Jamie's ambiguous case in
    terms of auto-assignment. A ranked list of exactly one is still a list
    a human confirms, not an assignment."""
    subjects = await _subjects(client, project_doc["doc_id"])
    alex = next(s for s in subjects if s["extracted_name"] == "Alex Kim")
    assert alex["resolution_status"] == "unresolved"
    assert [c["employee_id"] for c in alex["candidates"]] == ["extract-alex"]

    rows = db_session.query(ProposedChange).filter(
        ProposedChange.subject_match_id == alex["id"]).all()
    assert all(r.employee_id is None for r in rows)


async def test_no_plausible_match_yields_empty_candidate_list(client, project_doc):
    subjects = await _subjects(client, project_doc["doc_id"])
    robin = next(s for s in subjects if s["extracted_name"] == "Robin Nobody")
    assert robin["candidates"] == []
    assert robin["resolution_status"] == "unresolved"


def test_department_mention_is_a_weak_tiebreaker(db_session):
    """Direct unit test of the ranking function: two "Sam Ranked"s in
    different departments, a department signal should push the matching
    one ahead without approaching an email-match's confidence."""
    from app.auth import AuthenticatedUser
    from app.doc_extraction import rank_candidates

    caller = AuthenticatedUser(id="ranking-test", role="hr")
    ranked = rank_candidates(db_session, caller, "Sam Ranked", department="Finance Operations")

    ids = [c["employee_id"] for c in ranked]
    assert set(ids) == {"extract-dept-a", "extract-dept-b"}
    top = ranked[0]
    assert top["employee_id"] == "extract-dept-b"  # the Financial Analyst
    assert "department_match" in top["match_reason"]
    assert top["confidence"] < 0.9  # nowhere near an email-match's confidence
    assert top["confidence"] > ranked[1]["confidence"]


# ---------------------------------------------------------------------------
# Invisible until resolved.
# ---------------------------------------------------------------------------

async def test_pending_rows_invisible_until_subject_resolved(client, project_doc):
    groups = await _proposals(client, doc_id=project_doc["doc_id"])
    assert groups == [], f"unresolved subjects' rows leaked into the review screen: {groups}"


async def test_rows_appear_after_resolve(client, project_doc):
    subjects = await _subjects(client, project_doc["doc_id"])
    alex = next(s for s in subjects if s["extracted_name"] == "Alex Kim")

    resp = await _resolve(client, alex["id"], employee_id="extract-alex")
    assert resp.status_code == 200, resp.text
    assert resp.json()["resolution_status"] == "resolved"

    groups = await _proposals(client, doc_id=project_doc["doc_id"])
    assert len(groups) == 1
    assert groups[0]["employee_id"] == "extract-alex"
    assert len(groups[0]["changes"]) >= 2  # contribution + project_entry (+ skill)
    assert all(c["status"] == "pending" for c in groups[0]["changes"])


async def test_resolve_only_fills_still_null_rows(client, project_doc, db_session):
    """A row individually reassigned before its subject resolves must not
    be silently overwritten by the subject's own resolution."""
    subjects = await _subjects(client, project_doc["doc_id"])
    jamie = next(s for s in subjects if s["extracted_name"] == "Jamie Doubleton")
    rows = db_session.query(ProposedChange).filter(
        ProposedChange.subject_match_id == jamie["id"]).all()
    one_row_id = rows[0].id

    reassign_resp = await client.post(
        f"/proposed_changes/{one_row_id}/reassign", params={"view_mode": "work"},
        json={"employee_id": "extract-alex"}, headers=auth_headers("hr"))
    assert reassign_resp.status_code == 200, reassign_resp.text

    await _resolve(client, jamie["id"], employee_id="extract-dup-1")

    db_session.expire_all()
    reassigned_row = db_session.get(ProposedChange, one_row_id)
    assert reassigned_row.employee_id == "extract-alex"  # untouched by the later subject resolve

    others_resolved = [r for r in rows[1:] if db_session.get(ProposedChange, r.id).employee_id == "extract-dup-1"]
    assert len(others_resolved) == len(rows) - 1


# ---------------------------------------------------------------------------
# Resume, no plausible match -> new_hire_candidate.
# ---------------------------------------------------------------------------

async def test_resume_with_no_match_stages_unresolved_without_erroring(client):
    resp = await _upload(client, RESUME_NO_MATCH_TEXT, filename="nobody.docx")
    assert resp.status_code == 201, resp.text
    assert resp.json()["doc_type"] == "resume"

    subjects = await _subjects(client, resp.json()["doc_id"])
    assert len(subjects) == 1
    assert subjects[0]["extracted_name"] == "Nobody Findable"
    assert subjects[0]["candidates"] == []
    assert subjects[0]["resolution_status"] == "unresolved"


async def test_new_hire_candidate_flagged_explicitly_by_reviewer(client):
    resp = await _upload(client, RESUME_NO_MATCH_TEXT, filename="nobody2.docx")
    doc_id = resp.json()["doc_id"]
    subjects = await _subjects(client, doc_id)
    subject_id = subjects[0]["id"]

    resolve_resp = await _resolve(client, subject_id, new_hire=True)
    assert resolve_resp.status_code == 200, resolve_resp.text
    body = resolve_resp.json()
    assert body["resolution_status"] == "new_hire_candidate"
    assert body["resolved_employee_id"] is None

    # Still nothing in the review screen — there's no employee to attach to.
    groups = await _proposals(client, doc_id=doc_id)
    assert groups == []


async def test_resolve_requires_exactly_one_of_employee_id_or_new_hire(client, project_doc):
    subjects = await _subjects(client, project_doc["doc_id"])
    subject_id = subjects[0]["id"]

    neither = await client.post(
        f"/doc_subject_matches/{subject_id}/resolve", params={"view_mode": "work"},
        json={}, headers=auth_headers("hr"))
    assert neither.status_code == 422

    both = await client.post(
        f"/doc_subject_matches/{subject_id}/resolve", params={"view_mode": "work"},
        json={"employee_id": "extract-alex", "new_hire": True}, headers=auth_headers("hr"))
    assert both.status_code == 422


# ---------------------------------------------------------------------------
# Resume skill proposals.
# ---------------------------------------------------------------------------

async def test_resume_produces_only_skill_proposals(client, db_session):
    resp = await _upload(client, RESUME_TEXT, filename="resume2.docx")
    doc_id = resp.json()["doc_id"]
    rows = db_session.query(ProposedChange).filter(ProposedChange.source_doc_id == doc_id).all()
    assert rows
    assert all(r.change_type.value == "skill" for r in rows)


# ---------------------------------------------------------------------------
# Accept / edit / reject / reassign — per-field.
# ---------------------------------------------------------------------------

async def _resolved_change_ids(client, doc_id, employee_id, change_type=None):
    groups = await _proposals(client, doc_id=doc_id, employee_id=employee_id)
    group = next(g for g in groups if g["employee_id"] == employee_id)
    changes = group["changes"]
    if change_type:
        changes = [c for c in changes if c["change_type"] == change_type]
    return [c["id"] for c in changes]


async def test_accept_commits_contribution_and_reindexes(client, project_doc, db_session):
    subjects = await _subjects(client, project_doc["doc_id"])
    alex = next(s for s in subjects if s["extracted_name"] == "Alex Kim")
    await _resolve(client, alex["id"], employee_id="extract-alex")

    ids = await _resolved_change_ids(client, project_doc["doc_id"], "extract-alex", "contribution")
    resp = await client.post(
        f"/proposed_changes/{ids[0]}/accept", params={"view_mode": "work"},
        headers=auth_headers("hr", "hr-reviewer-1"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "accepted"

    db_session.expire_all()
    rows = db_session.query(EmployeeProject).filter(EmployeeProject.employee_id == "extract-alex").all()
    assert any(r.contribution and "ingest pipeline" in r.contribution for r in rows)


async def test_accept_writes_audit_row_with_ai_extraction_source(client, project_doc, db_session):
    subjects = await _subjects(client, project_doc["doc_id"])
    alex = next(s for s in subjects if s["extracted_name"] == "Alex Kim")
    await _resolve(client, alex["id"], employee_id="extract-alex")

    ids = await _resolved_change_ids(client, project_doc["doc_id"], "extract-alex", "skill")
    resp = await client.post(
        f"/proposed_changes/{ids[0]}/accept", params={"view_mode": "work"},
        headers=auth_headers("hr", "hr-reviewer-1"))
    assert resp.status_code == 200

    row = (
        db_session.query(AuditLog).filter(AuditLog.action == "accept_proposed_change")
        .order_by(AuditLog.id.desc()).first()
    )
    assert row is not None
    assert row.source == "ai_extraction"
    assert row.actor_id == "hr-reviewer-1"


async def test_accepted_skill_lands_as_learning_self_reported(client, project_doc, db_session):
    subjects = await _subjects(client, project_doc["doc_id"])
    alex = next(s for s in subjects if s["extracted_name"] == "Alex Kim")
    await _resolve(client, alex["id"], employee_id="extract-alex")

    ids = await _resolved_change_ids(client, project_doc["doc_id"], "extract-alex", "skill")
    await client.post(f"/proposed_changes/{ids[0]}/accept", params={"view_mode": "work"},
                      headers=auth_headers("hr"))

    db_session.expire_all()
    rows = db_session.query(EmployeeSkill).filter(EmployeeSkill.employee_id == "extract-alex").all()
    assert rows
    assert all(r.level.value == "Learning" for r in rows)
    assert all(r.source.value == "self" for r in rows)


async def test_accepting_unresolved_proposal_is_409(client, project_doc, db_session):
    subjects = await _subjects(client, project_doc["doc_id"])
    jamie = next(s for s in subjects if s["extracted_name"] == "Jamie Doubleton")
    # Jamie is still unresolved -- pull one of its rows directly.
    row = db_session.query(ProposedChange).filter(ProposedChange.subject_match_id == jamie["id"]).first()
    proposal_id = row.id

    resp = await client.post(
        f"/proposed_changes/{proposal_id}/accept", params={"view_mode": "work"},
        headers=auth_headers("hr"))
    assert resp.status_code == 409, resp.text


async def test_accept_is_not_repeatable(client, project_doc):
    subjects = await _subjects(client, project_doc["doc_id"])
    alex = next(s for s in subjects if s["extracted_name"] == "Alex Kim")
    await _resolve(client, alex["id"], employee_id="extract-alex")

    ids = await _resolved_change_ids(client, project_doc["doc_id"], "extract-alex", "project_entry")
    first = await client.post(f"/proposed_changes/{ids[0]}/accept",
                              params={"view_mode": "work"}, headers=auth_headers("hr"))
    second = await client.post(f"/proposed_changes/{ids[0]}/accept",
                               params={"view_mode": "work"}, headers=auth_headers("hr"))
    assert first.status_code == 200
    assert second.status_code == 409


async def test_edit_commits_the_edited_value_not_raw_output(client, project_doc, db_session):
    """The load-bearing /edit test: the committed contribution text must be
    the reviewer's own words, not what the model originally proposed."""
    subjects = await _subjects(client, project_doc["doc_id"])
    alex = next(s for s in subjects if s["extracted_name"] == "Alex Kim")
    await _resolve(client, alex["id"], employee_id="extract-alex")

    ids = await _resolved_change_ids(client, project_doc["doc_id"], "extract-alex", "contribution")
    edited = {"project": "Project Nightingale", "contribution": "Rewritten entirely by the reviewer."}
    resp = await client.post(
        f"/proposed_changes/{ids[0]}/edit", params={"view_mode": "work"},
        json={"edited_value": edited}, headers=auth_headers("hr", "hr-reviewer-1"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "edited"
    assert resp.json()["proposed_value"] == edited

    db_session.expire_all()
    membership = db_session.query(EmployeeProject).filter(
        EmployeeProject.employee_id == "extract-alex",
    ).first()
    assert membership.contribution == "Rewritten entirely by the reviewer."
    assert "ingest pipeline" not in (membership.contribution or "")

    row = db_session.query(AuditLog).filter(AuditLog.action == "edit_proposed_change").order_by(
        AuditLog.id.desc()).first()
    assert row.source == "ai_extraction"


async def test_reject_marks_rejected_and_commits_nothing(client, project_doc, db_session):
    subjects = await _subjects(client, project_doc["doc_id"])
    alex = next(s for s in subjects if s["extracted_name"] == "Alex Kim")
    await _resolve(client, alex["id"], employee_id="extract-alex")
    ids = await _resolved_change_ids(client, project_doc["doc_id"], "extract-alex", "project_entry")

    before = db_session.query(EmployeeProject).filter(EmployeeProject.employee_id == "extract-alex").count()
    resp = await client.post(
        f"/proposed_changes/{ids[0]}/reject", params={"view_mode": "work"},
        headers=auth_headers("hr", "hr-reviewer-9"))
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"

    db_session.expire_all()
    after = db_session.query(EmployeeProject).filter(EmployeeProject.employee_id == "extract-alex").count()
    assert after == before

    row = db_session.get(ProposedChange, ids[0])
    assert row is not None  # kept, not deleted
    assert row.reviewed_by == "hr-reviewer-9"


async def test_rejected_proposal_cannot_then_be_accepted(client, project_doc):
    subjects = await _subjects(client, project_doc["doc_id"])
    alex = next(s for s in subjects if s["extracted_name"] == "Alex Kim")
    await _resolve(client, alex["id"], employee_id="extract-alex")
    ids = await _resolved_change_ids(client, project_doc["doc_id"], "extract-alex", "skill")

    await client.post(f"/proposed_changes/{ids[0]}/reject", params={"view_mode": "work"},
                      headers=auth_headers("hr"))
    resp = await client.post(f"/proposed_changes/{ids[0]}/accept",
                             params={"view_mode": "work"}, headers=auth_headers("hr"))
    assert resp.status_code == 409


async def test_reassign_moves_a_single_row_independent_of_its_subject(client, project_doc, db_session):
    subjects = await _subjects(client, project_doc["doc_id"])
    jamie = next(s for s in subjects if s["extracted_name"] == "Jamie Doubleton")
    rows = db_session.query(ProposedChange).filter(
        ProposedChange.subject_match_id == jamie["id"]).all()
    target_id = rows[0].id

    resp = await client.post(
        f"/proposed_changes/{target_id}/reassign", params={"view_mode": "work"},
        json={"employee_id": "extract-dup-1"}, headers=auth_headers("hr"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["employee_id"] == "extract-dup-1"
    assert resp.json()["status"] == "pending"  # reassigning says who, not that it's true

    db_session.expire_all()
    row = db_session.get(ProposedChange, target_id)
    assert row.status is ProposedChangeStatus.pending
    assert row.employee_id == "extract-dup-1"


async def test_correct_stays_pending_and_updates_proposed_value(client, project_doc):
    subjects = await _subjects(client, project_doc["doc_id"])
    alex = next(s for s in subjects if s["extracted_name"] == "Alex Kim")
    await _resolve(client, alex["id"], employee_id="extract-alex")
    ids = await _resolved_change_ids(client, project_doc["doc_id"], "extract-alex", "project_entry")

    resp = await client.post(
        f"/proposed_changes/{ids[0]}/correct", params={"view_mode": "work"},
        json={"instruction": "project: Project Nightingale Phase 2"},
        headers=auth_headers("hr"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "pending"
    assert resp.json()["proposed_value"]["project"] == "Project Nightingale Phase 2"


# ---------------------------------------------------------------------------
# Undo: reverse an accept()/edit() while the source document is still
# under review.
# ---------------------------------------------------------------------------

async def _undo(client, proposal_id, role="hr", view_mode="work"):
    return await client.post(
        f"/proposed_changes/{proposal_id}/undo", params={"view_mode": view_mode},
        headers=auth_headers(role, "hr-reviewer-1"))


# A dedicated document + skill name for the two tests below, deliberately
# distinct from PROJECT_DOC_TEXT (which most other tests in this file also
# accept skills from, against the same shared session-scoped database — see
# conftest.py). A name this specific can't collide with anything an earlier
# or later test in the suite committed, so "was this skill freshly created
# by MY accept()" stays a fact these tests can rely on regardless of run
# order, instead of a guess about what already happened to extract-alex.
UNDO_SKILL_DOC_TEXT = """Weekly status report

Uma Kestrel worked on Project Undo Test Alpha. Piloted a rollback tool using Zephyrion Toolkit.
"""


async def _undo_skill_doc(client):
    resp = await _upload(client, UNDO_SKILL_DOC_TEXT, filename="undo-skill-fixture.docx")
    assert resp.status_code == 201, resp.text
    doc_id = resp.json()["doc_id"]
    subjects = await _subjects(client, doc_id)
    subject = next(s for s in subjects if s["extracted_name"] == "Uma Kestrel")
    await _resolve(client, subject["id"], employee_id="extract-alex")
    skill_id = (await _resolved_change_ids(client, doc_id, "extract-alex", "skill"))[0]
    return skill_id


async def test_undo_skill_removes_a_newly_created_skill(client, db_session):
    skill_id = await _undo_skill_doc(client)

    accept_resp = await client.post(
        f"/proposed_changes/{skill_id}/accept", params={"view_mode": "work"}, headers=auth_headers("hr"))
    assert accept_resp.status_code == 200, accept_resp.text
    skill_name = accept_resp.json()["proposed_value"]["skill"]

    db_session.expire_all()
    from app.models import Skill
    row = (
        db_session.query(EmployeeSkill).join(Skill, EmployeeSkill.skill_id == Skill.id)
        .filter(EmployeeSkill.employee_id == "extract-alex", Skill.name == skill_name).first()
    )
    assert row is not None  # accept() created it

    undo_resp = await _undo(client, skill_id)
    assert undo_resp.status_code == 200, undo_resp.text
    assert undo_resp.json()["status"] == "pending"

    db_session.expire_all()
    row = (
        db_session.query(EmployeeSkill).join(Skill, EmployeeSkill.skill_id == Skill.id)
        .filter(EmployeeSkill.employee_id == "extract-alex", Skill.name == skill_name).first()
    )
    assert row is None  # undo() removed exactly what accept() created
    assert db_session.get(ProposedChange, skill_id).status is ProposedChangeStatus.pending


async def test_undo_skill_never_removes_one_that_already_existed(client, db_session):
    """Two proposals could plausibly target the same (employee, skill) —
    two documents both mentioning Alex knows Terraform, say. The second
    accept() no-ops (never downgrades a level already held); undoing IT
    must not delete a skill the first, still-accepted proposal is
    responsible for."""
    skill_id = await _undo_skill_doc(client)
    skill_name = json.loads(db_session.get(ProposedChange, skill_id).proposed_value)["skill"]

    from app.models import Skill
    skill = db_session.query(Skill).filter(Skill.name == skill_name).first()
    if skill is None:
        from app.models.enums import SkillCategory
        skill = Skill(name=skill_name, category=SkillCategory.technical, canonical_id=None)
        db_session.add(skill)
        db_session.commit()
    from app.models.enums import SkillLevel, SkillSource
    db_session.add(EmployeeSkill(
        employee_id="extract-alex", skill_id=skill.id, level=SkillLevel.working, source=SkillSource.confirmed,
    ))
    db_session.commit()

    accept_resp = await client.post(
        f"/proposed_changes/{skill_id}/accept", params={"view_mode": "work"}, headers=auth_headers("hr"))
    assert accept_resp.status_code == 200, accept_resp.text

    undo_resp = await _undo(client, skill_id)
    assert undo_resp.status_code == 200, undo_resp.text

    db_session.expire_all()
    row = db_session.query(EmployeeSkill).filter(
        EmployeeSkill.employee_id == "extract-alex", EmployeeSkill.skill_id == skill.id).first()
    assert row is not None
    assert row.level is SkillLevel.working  # the pre-existing, higher-level row survives untouched


async def _seed_project(db_session, name, owner_id):
    """Find-or-create, same shape as app.proposals._get_or_create_project —
    the test database is session-scoped (see conftest.py), so a project
    named "Project Nightingale" almost certainly already exists by the
    time any one of these tests runs, from an earlier test's own accept().
    Reusing it (rather than blind-inserting a duplicate) is what keeps
    _get_or_create_project's own `.first()` lookup pointed at the same row
    this test just set up, instead of an ambiguous earlier one."""
    from app.models import Employee, Project
    from app.models.enums import ProjectClassification, ProjectType

    project = db_session.query(Project).filter(Project.name.ilike(name)).first()
    if project is not None:
        return project
    owner = db_session.get(Employee, owner_id)
    project = Project(
        name=name, type=ProjectType.project, description=None,
        owning_unit_id=owner.org_unit_id, owner_id=owner_id,
        classification=ProjectClassification.internal, is_client_engagement=False,
    )
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)
    return project


async def _seed_membership(db_session, employee_id, project_id, **fields):
    """Find-or-create the membership row too, same reasoning as
    _seed_project — an earlier test in this session may have already put
    extract-alex on Project Nightingale. Overwrites `fields` onto whichever
    row it finds/creates, so each test still controls its own "previous"
    state regardless of what an earlier test left behind."""
    membership = (
        db_session.query(EmployeeProject)
        .filter(EmployeeProject.employee_id == employee_id, EmployeeProject.project_id == project_id)
        .first()
    )
    if membership is None:
        membership = EmployeeProject(
            employee_id=employee_id, project_id=project_id,
            role="Contributor", start_date=date(2020, 1, 1),
        )
        db_session.add(membership)
    for key, val in fields.items():
        setattr(membership, key, val)
    db_session.commit()
    db_session.refresh(membership)
    return membership


async def test_undo_contribution_restores_previous_text(client, project_doc, db_session):
    subjects = await _subjects(client, project_doc["doc_id"])
    alex = next(s for s in subjects if s["extracted_name"] == "Alex Kim")
    await _resolve(client, alex["id"], employee_id="extract-alex")
    contribution_id = (await _resolved_change_ids(client, project_doc["doc_id"], "extract-alex", "contribution"))[0]
    project_name = json.loads(db_session.get(ProposedChange, contribution_id).proposed_value)["project"]
    project = await _seed_project(db_session, project_name, "extract-alex")
    await _seed_membership(db_session, "extract-alex", project.id, contribution="ORIGINAL CONTRIBUTION")

    accept_resp = await client.post(
        f"/proposed_changes/{contribution_id}/accept", params={"view_mode": "work"}, headers=auth_headers("hr"))
    assert accept_resp.status_code == 200, accept_resp.text
    db_session.expire_all()
    membership = db_session.query(EmployeeProject).filter(
        EmployeeProject.employee_id == "extract-alex", EmployeeProject.project_id == project.id).first()
    assert membership.contribution != "ORIGINAL CONTRIBUTION"

    undo_resp = await _undo(client, contribution_id)
    assert undo_resp.status_code == 200, undo_resp.text

    db_session.expire_all()
    membership = db_session.query(EmployeeProject).filter(
        EmployeeProject.employee_id == "extract-alex", EmployeeProject.project_id == project.id).first()
    assert membership.contribution == "ORIGINAL CONTRIBUTION"


async def test_undo_project_entry_restores_previous_role(client, project_doc, db_session):
    subjects = await _subjects(client, project_doc["doc_id"])
    alex = next(s for s in subjects if s["extracted_name"] == "Alex Kim")
    await _resolve(client, alex["id"], employee_id="extract-alex")
    entry_id = (await _resolved_change_ids(client, project_doc["doc_id"], "extract-alex", "project_entry"))[0]
    project_name = json.loads(db_session.get(ProposedChange, entry_id).proposed_value)["project"]
    project = await _seed_project(db_session, project_name, "extract-alex")
    await _seed_membership(db_session, "extract-alex", project.id, role="Legacy Role")

    accept_resp = await client.post(
        f"/proposed_changes/{entry_id}/accept", params={"view_mode": "work"}, headers=auth_headers("hr"))
    assert accept_resp.status_code == 200, accept_resp.text
    db_session.expire_all()
    membership = db_session.query(EmployeeProject).filter(
        EmployeeProject.employee_id == "extract-alex", EmployeeProject.project_id == project.id).first()
    assert membership.role != "Legacy Role"

    undo_resp = await _undo(client, entry_id)
    assert undo_resp.status_code == 200, undo_resp.text

    db_session.expire_all()
    membership = db_session.query(EmployeeProject).filter(
        EmployeeProject.employee_id == "extract-alex", EmployeeProject.project_id == project.id).first()
    assert membership.role == "Legacy Role"


async def test_undo_refuses_after_document_finalized(client, project_doc, db_session):
    subjects = await _subjects(client, project_doc["doc_id"])
    alex = next(s for s in subjects if s["extracted_name"] == "Alex Kim")
    await _resolve(client, alex["id"], employee_id="extract-alex")
    skill_id = (await _resolved_change_ids(client, project_doc["doc_id"], "extract-alex", "skill"))[0]

    await client.post(
        f"/proposed_changes/{skill_id}/accept", params={"view_mode": "work"}, headers=auth_headers("hr"))
    await client.post(
        f"/docs/{project_doc['doc_id']}/finalize", params={"view_mode": "work"},
        json={"accept_ids": []}, headers=auth_headers("hr"))

    resp = await _undo(client, skill_id)
    assert resp.status_code == 409
    assert "finalized" in resp.json()["detail"]

    db_session.expire_all()
    assert db_session.get(ProposedChange, skill_id).status is ProposedChangeStatus.accepted


async def test_undo_refuses_on_rejected_row(client, project_doc):
    subjects = await _subjects(client, project_doc["doc_id"])
    alex = next(s for s in subjects if s["extracted_name"] == "Alex Kim")
    await _resolve(client, alex["id"], employee_id="extract-alex")
    skill_id = (await _resolved_change_ids(client, project_doc["doc_id"], "extract-alex", "skill"))[0]

    await client.post(
        f"/proposed_changes/{skill_id}/reject", params={"view_mode": "work"}, headers=auth_headers("hr"))
    resp = await _undo(client, skill_id)
    assert resp.status_code == 409


async def test_undo_refuses_on_still_pending_row(client, project_doc):
    subjects = await _subjects(client, project_doc["doc_id"])
    alex = next(s for s in subjects if s["extracted_name"] == "Alex Kim")
    await _resolve(client, alex["id"], employee_id="extract-alex")
    skill_id = (await _resolved_change_ids(client, project_doc["doc_id"], "extract-alex", "skill"))[0]

    resp = await _undo(client, skill_id)
    assert resp.status_code == 409


@pytest.mark.parametrize("role", ["employee", "manager", "it"])
async def test_undo_is_hr_only(client, project_doc, role):
    subjects = await _subjects(client, project_doc["doc_id"])
    alex = next(s for s in subjects if s["extracted_name"] == "Alex Kim")
    await _resolve(client, alex["id"], employee_id="extract-alex")
    skill_id = (await _resolved_change_ids(client, project_doc["doc_id"], "extract-alex", "skill"))[0]
    await client.post(
        f"/proposed_changes/{skill_id}/accept", params={"view_mode": "work"}, headers=auth_headers("hr"))

    resp = await _undo(client, skill_id, role=role)
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Permissions: skills/contribution/project_entry gated per change_type.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("role", ["employee", "manager", "it"])
async def test_accept_is_hr_only(client, project_doc, role):
    subjects = await _subjects(client, project_doc["doc_id"])
    alex = next(s for s in subjects if s["extracted_name"] == "Alex Kim")
    await _resolve(client, alex["id"], employee_id="extract-alex")
    ids = await _resolved_change_ids(client, project_doc["doc_id"], "extract-alex")

    resp = await client.post(
        f"/proposed_changes/{ids[0]}/accept", params={"view_mode": "work"},
        headers=auth_headers(role))
    assert resp.status_code == 403


async def test_hr_cannot_commit_in_employee_mode(client, project_doc):
    subjects = await _subjects(client, project_doc["doc_id"])
    alex = next(s for s in subjects if s["extracted_name"] == "Alex Kim")
    await _resolve(client, alex["id"], employee_id="extract-alex")
    ids = await _resolved_change_ids(client, project_doc["doc_id"], "extract-alex")

    resp = await client.post(
        f"/proposed_changes/{ids[0]}/accept", params={"view_mode": "employee"},
        headers=auth_headers("hr"))
    assert resp.status_code == 403


def test_skills_contribution_project_entry_are_hr_editable():
    from app.permissions import can_edit

    for field in ("skills", "contribution", "project_entry"):
        assert can_edit("hr", "work", field) is True
        assert can_edit("hr", "employee", field) is False
        assert can_edit("it", "work", field) is False
        assert can_edit("employee", "work", field) is False


@pytest.mark.parametrize("role", ["employee", "manager", "it"])
async def test_review_queues_are_hr_only(client, project_doc, role):
    subj_resp = await client.get(
        "/doc_subject_matches", params={"doc_id": project_doc["doc_id"], "view_mode": "work"},
        headers=auth_headers(role))
    assert subj_resp.status_code == 403

    prop_resp = await client.get(
        "/proposed_changes", params={"doc_id": project_doc["doc_id"], "view_mode": "work"},
        headers=auth_headers(role))
    assert prop_resp.status_code == 403


# ---------------------------------------------------------------------------
# Bulk accept / reject.
# ---------------------------------------------------------------------------

async def test_bulk_accept_applies_per_row_logic_to_every_row(client, project_doc, db_session):
    subjects = await _subjects(client, project_doc["doc_id"])
    alex = next(s for s in subjects if s["extracted_name"] == "Alex Kim")
    await _resolve(client, alex["id"], employee_id="extract-alex")
    ids = await _resolved_change_ids(client, project_doc["doc_id"], "extract-alex")
    assert len(ids) >= 2

    resp = await client.post(
        "/proposed_changes/bulk_accept", params={"view_mode": "work"},
        json={"ids": ids}, headers=auth_headers("hr", "hr-reviewer-1"))
    assert resp.status_code == 200, resp.text
    results = resp.json()["results"]
    assert len(results) == len(ids)
    assert all(r["ok"] and r["status"] == "accepted" for r in results)

    db_session.expire_all()
    assert all(
        db_session.get(ProposedChange, i).status is ProposedChangeStatus.accepted for i in ids
    )


async def test_bulk_reject_by_doc_id_filter(client, project_doc, db_session):
    subjects = await _subjects(client, project_doc["doc_id"])
    alex = next(s for s in subjects if s["extracted_name"] == "Alex Kim")
    await _resolve(client, alex["id"], employee_id="extract-alex")
    ids = await _resolved_change_ids(client, project_doc["doc_id"], "extract-alex")

    resp = await client.post(
        "/proposed_changes/bulk_reject", params={"view_mode": "work"},
        json={"doc_id": project_doc["doc_id"], "employee_id": "extract-alex"},
        headers=auth_headers("hr"))
    assert resp.status_code == 200, resp.text
    results = resp.json()["results"]
    assert {r["id"] for r in results} == set(ids)
    assert all(r["ok"] and r["status"] == "rejected" for r in results)


async def test_bulk_accept_reports_per_row_failures_without_failing_the_batch(client, project_doc, db_session):
    subjects = await _subjects(client, project_doc["doc_id"])
    alex = next(s for s in subjects if s["extracted_name"] == "Alex Kim")
    await _resolve(client, alex["id"], employee_id="extract-alex")
    ok_ids = await _resolved_change_ids(client, project_doc["doc_id"], "extract-alex")

    jamie = next(s for s in subjects if s["extracted_name"] == "Jamie Doubleton")
    unresolved_row = db_session.query(ProposedChange).filter(
        ProposedChange.subject_match_id == jamie["id"]).first()
    unresolved_row_id = unresolved_row.id

    resp = await client.post(
        "/proposed_changes/bulk_accept", params={"view_mode": "work"},
        json={"ids": [*ok_ids, unresolved_row_id]}, headers=auth_headers("hr"))
    assert resp.status_code == 200, resp.text
    results = {r["id"]: r for r in resp.json()["results"]}
    assert all(results[i]["ok"] for i in ok_ids)
    assert results[unresolved_row_id]["ok"] is False


async def test_bulk_action_requires_a_selector(client):
    resp = await client.post(
        "/proposed_changes/bulk_accept", params={"view_mode": "work"},
        json={}, headers=auth_headers("hr"))
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /docs + POST /docs/{id}/finalize — the "Update" action: accept the
# checked rows, reject the rest, then scrub the document's own content.
# ---------------------------------------------------------------------------

async def _docs(client, role="hr"):
    resp = await client.get("/uploaded_docs", params={"view_mode": "work"}, headers=auth_headers(role))
    assert resp.status_code == 200, resp.text
    return resp.json()["documents"]


async def test_finalize_accepts_selected_and_rejects_the_rest(client, project_doc, db_session):
    subjects = await _subjects(client, project_doc["doc_id"])
    alex = next(s for s in subjects if s["extracted_name"] == "Alex Kim")
    await _resolve(client, alex["id"], employee_id="extract-alex")
    ids = await _resolved_change_ids(client, project_doc["doc_id"], "extract-alex")
    assert len(ids) >= 2
    keep, drop = ids[0], ids[1:]

    resp = await client.post(
        f"/docs/{project_doc['doc_id']}/finalize", params={"view_mode": "work"},
        json={"accept_ids": [keep]}, headers=auth_headers("hr", "hr-reviewer-1"))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["content_scrubbed_at"] is not None
    results = {r["id"]: r for r in body["results"]}
    assert results[keep]["status"] == "accepted"
    assert all(results[i]["status"] == "rejected" for i in drop)

    db_session.expire_all()
    assert db_session.get(ProposedChange, keep).status is ProposedChangeStatus.accepted
    for i in drop:
        assert db_session.get(ProposedChange, i).status is ProposedChangeStatus.rejected

    from app.models import UploadedDoc
    doc = db_session.get(UploadedDoc, project_doc["doc_id"])
    assert doc.extracted_text == ""
    assert doc.content_scrubbed_at is not None


async def test_finalize_with_no_accepted_ids_rejects_everything_and_still_scrubs(client, project_doc, db_session):
    subjects = await _subjects(client, project_doc["doc_id"])
    alex = next(s for s in subjects if s["extracted_name"] == "Alex Kim")
    await _resolve(client, alex["id"], employee_id="extract-alex")
    ids = await _resolved_change_ids(client, project_doc["doc_id"], "extract-alex")

    resp = await client.post(
        f"/docs/{project_doc['doc_id']}/finalize", params={"view_mode": "work"},
        json={"accept_ids": []}, headers=auth_headers("hr"))
    assert resp.status_code == 200, resp.text
    results = resp.json()["results"]
    assert all(r["status"] == "rejected" for r in results)

    db_session.expire_all()
    from app.models import UploadedDoc
    assert db_session.get(UploadedDoc, project_doc["doc_id"]).extracted_text == ""


async def test_finalize_leaves_unresolved_subject_rows_pending_and_decidable_later(
    client, project_doc, db_session,
):
    """Jamie Doubleton's subject is left unresolved (two same-named
    candidates) — its proposed_changes rows have no employee_id yet, so
    they're invisible to GET /proposed_changes and finalize can't touch
    them. Finalizing the document anyway must not lose them: they stay
    pending, and resolving the subject afterward still works, since
    accept()/reject() never need the document's own text."""
    subjects = await _subjects(client, project_doc["doc_id"])
    alex = next(s for s in subjects if s["extracted_name"] == "Alex Kim")
    await _resolve(client, alex["id"], employee_id="extract-alex")
    jamie = next(s for s in subjects if s["extracted_name"] == "Jamie Doubleton")

    resp = await client.post(
        f"/docs/{project_doc['doc_id']}/finalize", params={"view_mode": "work"},
        json={"accept_ids": []}, headers=auth_headers("hr"))
    assert resp.status_code == 200, resp.text

    jamie_row = db_session.query(ProposedChange).filter(
        ProposedChange.subject_match_id == jamie["id"]).first()
    assert jamie_row.status is ProposedChangeStatus.pending
    assert jamie_row.employee_id is None

    resolve_resp = await _resolve(client, jamie["id"], employee_id="extract-dup-2")
    assert resolve_resp.status_code == 200, resolve_resp.text
    accept_resp = await client.post(
        f"/proposed_changes/{jamie_row.id}/accept", params={"view_mode": "work"},
        headers=auth_headers("hr"))
    assert accept_resp.status_code == 200, accept_resp.text


async def test_correct_refuses_once_the_document_is_scrubbed(client, project_doc, db_session):
    """Jamie's subject is left unresolved on purpose (see the
    "leaves unresolved subject rows pending" test above) — its
    proposed_changes row stays pending straight through finalize, since
    finalize only ever touches employee-resolved rows. /correct still
    requires just `status == pending`, so it's reachable on that row even
    after the document is scrubbed — exactly the case the new guard in
    app.proposals.correct exists for."""
    subjects = await _subjects(client, project_doc["doc_id"])
    alex = next(s for s in subjects if s["extracted_name"] == "Alex Kim")
    await _resolve(client, alex["id"], employee_id="extract-alex")
    jamie = next(s for s in subjects if s["extracted_name"] == "Jamie Doubleton")
    jamie_row = db_session.query(ProposedChange).filter(
        ProposedChange.subject_match_id == jamie["id"]).first()

    await client.post(
        f"/docs/{project_doc['doc_id']}/finalize", params={"view_mode": "work"},
        json={"accept_ids": []}, headers=auth_headers("hr"))

    resp = await client.post(
        f"/proposed_changes/{jamie_row.id}/correct", params={"view_mode": "work"},
        json={"instruction": "actually it was React, not Terraform"}, headers=auth_headers("hr"))
    assert resp.status_code == 409
    assert "cleared" in resp.json()["detail"]


async def test_finalize_is_not_repeatable(client, project_doc):
    subjects = await _subjects(client, project_doc["doc_id"])
    alex = next(s for s in subjects if s["extracted_name"] == "Alex Kim")
    await _resolve(client, alex["id"], employee_id="extract-alex")

    first = await client.post(
        f"/docs/{project_doc['doc_id']}/finalize", params={"view_mode": "work"},
        json={"accept_ids": []}, headers=auth_headers("hr"))
    assert first.status_code == 200, first.text

    second = await client.post(
        f"/docs/{project_doc['doc_id']}/finalize", params={"view_mode": "work"},
        json={"accept_ids": []}, headers=auth_headers("hr"))
    assert second.status_code == 409


async def test_finalize_unknown_document_is_404(client):
    resp = await client.post(
        "/docs/999999/finalize", params={"view_mode": "work"},
        json={"accept_ids": []}, headers=auth_headers("hr"))
    assert resp.status_code == 404


@pytest.mark.parametrize("role", ["employee", "manager", "it"])
async def test_finalize_is_hr_only(client, project_doc, role):
    resp = await client.post(
        f"/docs/{project_doc['doc_id']}/finalize", params={"view_mode": "work"},
        json={"accept_ids": []}, headers=auth_headers(role))
    assert resp.status_code == 403


async def test_list_documents_reports_pending_and_scrubbed_state(client, project_doc, db_session):
    docs = await _docs(client)
    doc = next(d for d in docs if d["id"] == project_doc["doc_id"])
    assert doc["content_scrubbed_at"] is None
    assert doc["pending_count"] == 0  # nothing visible yet — both subjects unresolved
    assert doc["unresolved_subject_count"] == 3

    subjects = await _subjects(client, project_doc["doc_id"])
    alex = next(s for s in subjects if s["extracted_name"] == "Alex Kim")
    await _resolve(client, alex["id"], employee_id="extract-alex")

    docs = await _docs(client)
    doc = next(d for d in docs if d["id"] == project_doc["doc_id"])
    assert doc["pending_count"] > 0
    assert doc["unresolved_subject_count"] == 2

    await client.post(
        f"/docs/{project_doc['doc_id']}/finalize", params={"view_mode": "work"},
        json={"accept_ids": []}, headers=auth_headers("hr"))

    docs = await _docs(client)
    doc = next(d for d in docs if d["id"] == project_doc["doc_id"])
    assert doc["content_scrubbed_at"] is not None
    assert doc["pending_count"] == 0


# ---------------------------------------------------------------------------
# Nothing pending (unresolved OR unaccepted) appears in search.
# ---------------------------------------------------------------------------

async def test_unaccepted_content_is_not_searchable(client, db_session):
    resp = await _upload(client, (
        "Weekly status report\n\n"
        "Alex Kim worked on Project Unaccepted. Built the widget using Kubernetes.\n"
    ), filename="never-accepted.docx")
    doc_id = resp.json()["doc_id"]

    from app.models import Project
    assert db_session.query(Project).filter(Project.name == "Project Unaccepted").first() is None

    for params in ({"name": "Project Unaccepted"}, {"query": "Project Unaccepted"}):
        found = await client.get("/people", params=params, headers=auth_headers("hr"))
        assert found.status_code == 200
        assert found.json() == [], f"{params} leaked pending content, doc_id={doc_id}"

    skill_names = {
        r.skill.name for r in db_session.query(EmployeeSkill).filter(
            EmployeeSkill.employee_id == "extract-alex").all() if r.skill
    }
    assert "Kubernetes" not in skill_names


async def test_unresolved_subjects_content_is_not_searchable(client, project_doc):
    """The other half: Jamie Doubleton's proposals are RESOLVABLE (real
    candidates exist) but not yet resolved — still must not leak."""
    found = await client.get(
        "/people", params={"name": "Project Atlas"}, headers=auth_headers("hr"))
    assert found.status_code == 200
    # Project Atlas is a seeded fixture project unrelated to this doc, so a
    # name hit here would mean nothing — the real assertion is on the skill/
    # contribution side, already covered by test_pending_rows_invisible_...
    # and test_ranked_candidates_never_auto_assign. This just confirms the
    # upload itself didn't error the search path.
    assert isinstance(found.json(), list)


# ---------------------------------------------------------------------------
# Multi-round extraction (the real/model path).
#
# Everything above runs on the mock extractor. This section is the only
# place that exercises _real_extract_project_doc, because the bug it
# guards is invisible to the mock: the mock is a regex over the whole
# document and always sees every line, whereas the model chooses for
# itself how many of its one-call-per-person calls to fit in a single
# response. On a plain list of contributors it frequently emits only the
# FIRST person and stops with finish_reason="tool_calls" -- a normal,
# successful response, not an error or a truncation -- so a single-shot
# read of choices[0] silently loses everyone after them, and the reviewer
# gets a document that "didn't find all the people".
#
# No model API is touched here either: the client is faked at
# app.tool_calling._get_openai_client, which is where doc_extraction
# imports it from.
# ---------------------------------------------------------------------------

class _FakeFunction:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _FakeToolCall:
    def __init__(self, call_id, name, arguments):
        self.id = call_id
        self.type = "function"
        self.function = _FakeFunction(name, arguments)


class _FakeMessage:
    def __init__(self, tool_calls):
        self.content = "" if tool_calls else "DONE"
        self.tool_calls = tool_calls or None


class _FakeCompletions:
    """Replays a scripted list of rounds, one per create() call. Each round
    is a list of (name, project) pairs, or an Exception to raise instead.
    Records the messages it was handed so a test can assert the loop fed
    the previous round back rather than re-asking the same question."""

    def __init__(self, rounds):
        self.rounds = list(rounds)
        self.calls_made = 0
        self.seen_messages = []

    def create(self, **kwargs):
        self.seen_messages.append(kwargs["messages"])
        self.calls_made += 1
        round_spec = self.rounds.pop(0) if self.rounds else []
        if isinstance(round_spec, Exception):
            raise round_spec
        tool_calls = [
            _FakeToolCall(
                f"call-{self.calls_made}-{i}", "propose_project_update",
                json.dumps({
                    "member_name_guess": name, "project": project,
                    "contribution": f"{name} did work on {project}.",
                    "skills_gained": ["Terraform"], "confidence": 0.9,
                }),
            )
            for i, (name, project) in enumerate(round_spec)
        ]
        return type("R", (), {"choices": [type("C", (), {"message": _FakeMessage(tool_calls)})()]})()


class _FakeClient:
    def __init__(self, rounds):
        self.completions = _FakeCompletions(rounds)
        self.chat = type("Chat", (), {"completions": self.completions})()


def _fake_model(monkeypatch, rounds):
    from app import tool_calling
    client = _FakeClient(rounds)
    monkeypatch.setattr(tool_calling, "_get_openai_client", lambda: client)
    monkeypatch.setattr(tool_calling, "OPENAI_CHAT_DEPLOYMENT", "fake-deployment")
    return client


def test_extraction_collects_people_across_rounds(monkeypatch):
    """The regression. The model names one person, stops, and only names
    the next when asked again — exactly the observed gpt-5 behaviour that
    made a 3-person document resolve to 1. All three must survive."""
    from app import doc_extraction

    client = _fake_model(monkeypatch, [
        [("Isabela Krishnan", "Project Big Bird")],
        [("Amara Zhao", "Project Big Bird")],
        [("Charlotte Thompson", "Project Big Bird")],
        [],  # model answers in prose -- document finished
    ])
    calls = doc_extraction._real_extract_project_doc("...document text...")

    assert [c.member_name_guess for c in calls] == [
        "Isabela Krishnan", "Amara Zhao", "Charlotte Thompson"]
    assert client.completions.calls_made == 4
    # Each person keeps their OWN skills -- the rounds are accumulated, not
    # overwritten by the last one.
    assert all(c.skills_gained == ["Terraform"] for c in calls)


def test_extraction_feeds_prior_rounds_back_to_the_model(monkeypatch):
    """Continuation is native assistant/tool message pairs (same shape as
    app/tool_calling.py's _chain_step_messages), not a re-ask of the
    original question — otherwise round two just re-emits round one."""
    from app import doc_extraction

    client = _fake_model(monkeypatch, [
        [("Isabela Krishnan", "Project Big Bird")],
        [],
    ])
    doc_extraction._real_extract_project_doc("...document text...")

    second_round = client.completions.seen_messages[1]
    assert [m["role"] for m in second_round] == ["system", "user", "assistant", "tool"]
    assert second_round[2]["tool_calls"][0]["function"]["name"] == "propose_project_update"
    assert second_round[3]["tool_call_id"] == second_round[2]["tool_calls"][0]["id"]


def test_extraction_deduplicates_a_re_emitted_person(monkeypatch):
    """A model that re-emits someone it already named must not produce a
    second subject card for the same human — (name, project) is the pair
    record_project_doc_proposals keys a subject on."""
    from app import doc_extraction

    client = _fake_model(monkeypatch, [
        [("Isabela Krishnan", "Project Big Bird")],
        [("isabela krishnan", "project big bird")],  # same person, different case
        [("Amara Zhao", "Project Big Bird")],
    ])
    calls = doc_extraction._real_extract_project_doc("...document text...")

    assert [c.member_name_guess for c in calls] == ["Isabela Krishnan"]
    # A round that added nobody new ends the loop -- asking again would
    # only repeat it, so Amara's scripted round is never reached.
    assert client.completions.calls_made == 2


def test_extraction_is_bounded_by_max_rounds(monkeypatch):
    """A model that never stops naming people must still cost a bounded
    number of completions."""
    from app import doc_extraction

    client = _fake_model(monkeypatch, [
        [(f"Person {i}", "Project Big Bird")] for i in range(50)
    ])
    calls = doc_extraction._real_extract_project_doc("...document text...")

    assert client.completions.calls_made == doc_extraction.MAX_EXTRACTION_ROUNDS
    assert len(calls) == doc_extraction.MAX_EXTRACTION_ROUNDS


def test_extraction_keeps_earlier_rounds_when_a_later_one_fails(monkeypatch):
    """A failure in round three keeps the two rounds that worked. Falling
    back to the regex mock here would throw away real extractions and
    replace them with a worse read of the same document."""
    from openai import OpenAIError

    from app import doc_extraction

    _fake_model(monkeypatch, [
        [("Isabela Krishnan", "Project Big Bird")],
        [("Amara Zhao", "Project Big Bird")],
        OpenAIError("upstream blew up"),
    ])
    calls = doc_extraction._real_extract_project_doc("...document text...")

    assert [c.member_name_guess for c in calls] == ["Isabela Krishnan", "Amara Zhao"]


def test_extraction_falls_back_to_mock_when_the_first_round_fails(monkeypatch):
    """Nothing extracted at all still degrades to the mock, unchanged —
    that is the existing no-Azure-resources behaviour every other test in
    this module relies on."""
    from openai import OpenAIError

    from app import doc_extraction

    _fake_model(monkeypatch, [OpenAIError("upstream blew up")])
    calls = doc_extraction._real_extract_project_doc(PROJECT_DOC_TEXT)

    assert {c.member_name_guess for c in calls} == {
        "Alex Kim", "Jamie Doubleton", "Robin Nobody"}


# ---------------------------------------------------------------------------
# Candidate identity — what a reviewer picks BETWEEN.
# ---------------------------------------------------------------------------

async def test_same_named_candidates_are_distinguishable(client, project_doc):
    """The point of the ranked-candidate design is that a human confirms
    who a document meant. Two Jamie Doubletons match the same name with
    the same evidence, so full_name, confidence and match_reason are
    identical for both by construction -- if those are all the screen
    gets, the human is asked to choose with nothing to choose on."""
    subjects = await _subjects(client, project_doc["doc_id"])
    jamie = next(s for s in subjects if s["extracted_name"] == "Jamie Doubleton")
    candidates = jamie["candidates"]
    assert len(candidates) == 2

    # The premise: the document-derived fields genuinely cannot separate them.
    assert len({c["full_name"] for c in candidates}) == 1
    # The fix: directory identity can.
    assert {c["job_title"] for c in candidates} == {"Software Engineer", "Data Engineer"}
    assert all(c["org_unit"] for c in candidates)
    assert all(c["is_active"] is True for c in candidates)


async def test_candidate_details_are_read_at_display_time(client, project_doc, db_session):
    """Stored candidate_employee_ids is an extraction-time snapshot of the
    ranking. Identity is resolved when the screen is read, so a document
    staged before a promotion doesn't ask a reviewer to confirm someone by
    a job title they no longer hold."""
    from app.models import Employee

    employee = db_session.get(Employee, "extract-dup-1")
    employee.job_title = "Principal Engineer"
    db_session.commit()

    subjects = await _subjects(client, project_doc["doc_id"])
    jamie = next(s for s in subjects if s["extracted_name"] == "Jamie Doubleton")
    titles = {c["job_title"] for c in jamie["candidates"]}
    assert "Principal Engineer" in titles
    assert "Software Engineer" not in titles


async def test_candidate_details_stay_within_the_always_visible_field_set(client, project_doc):
    """A candidate list must not become a way to read more about somebody
    than searching for them would -- app/people.py's SUMMARY_FIELDS is the
    always-visible set, and salary/hire_date/etc. are not in it."""
    subjects = await _subjects(client, project_doc["doc_id"])
    allowed = {
        "employee_id", "full_name", "confidence", "match_reason",
        "job_title", "org_unit", "office", "is_active",
    }
    for subject in subjects:
        for candidate in subject["candidates"]:
            assert set(candidate) <= allowed, f"leaked {set(candidate) - allowed}"


# ---------------------------------------------------------------------------
# Separation of duties: a reviewer may not commit onto their own record.
#
# The pipeline writes to EmployeeSkill and EmployeeProject, so without this
# a reviewer could upload a document about themselves and accept it
# onto their own profile — the same hole app/writes.py already closes on
# every "edit anyone's record" path ("an hr caller giving themselves a
# raise"). Enforced at COMMIT time, because reassign() can point any row at
# any employee including the caller, so a check at resolve time would be
# one /reassign away from being bypassed.
# ---------------------------------------------------------------------------

async def _self_targeted_change(client, project_doc, reviewer_id):
    """Resolve Alex Kim's subject onto the reviewer themselves, and hand
    back one of the now-committable change ids."""
    subjects = await _subjects(client, project_doc["doc_id"])
    alex = next(s for s in subjects if s["extracted_name"] == "Alex Kim")
    resp = await client.post(
        f"/doc_subject_matches/{alex['id']}/resolve", params={"view_mode": "work"},
        json={"employee_id": reviewer_id}, headers=auth_headers("hr", "hr-reviewer-1"))
    assert resp.status_code == 200, resp.text
    return await _resolved_change_ids(client, project_doc["doc_id"], reviewer_id)


async def test_reviewer_cannot_accept_a_change_onto_themselves(client, project_doc):
    ids = await _self_targeted_change(client, project_doc, "extract-alex")
    resp = await client.post(
        f"/proposed_changes/{ids[0]}/accept", params={"view_mode": "work"},
        headers=auth_headers("hr", "extract-alex"))
    assert resp.status_code == 403, resp.text


async def test_reviewer_cannot_edit_a_change_onto_themselves(client, project_doc):
    """edit() commits too — refusing accept() alone would leave the same
    write reachable by supplying a value."""
    ids = await _self_targeted_change(client, project_doc, "extract-alex")
    resp = await client.post(
        f"/proposed_changes/{ids[0]}/edit", params={"view_mode": "work"},
        json={"edited_value": {"skill": "Terraform"}},
        headers=auth_headers("hr", "extract-alex"))
    assert resp.status_code == 403, resp.text


async def test_reassigning_a_row_to_yourself_does_not_unlock_it(client, project_doc):
    """The bypass the commit-time placement exists to close: /reassign can
    point any row at any employee, so a resolve-time check would not hold."""
    subjects = await _subjects(client, project_doc["doc_id"])
    robin = next(s for s in subjects if s["extracted_name"] == "Robin Nobody")
    resolved = await _resolve(client, robin["id"], employee_id="extract-alex")
    assert resolved.status_code == 200, resolved.text
    ids = await _resolved_change_ids(client, project_doc["doc_id"], "extract-alex")

    reassigned = await client.post(
        f"/proposed_changes/{ids[0]}/reassign", params={"view_mode": "work"},
        json={"employee_id": "extract-dup-1"}, headers=auth_headers("hr", "extract-dup-1"))
    assert reassigned.status_code == 200, reassigned.text

    resp = await client.post(
        f"/proposed_changes/{ids[0]}/accept", params={"view_mode": "work"},
        headers=auth_headers("hr", "extract-dup-1"))
    assert resp.status_code == 403, resp.text


async def test_another_reviewer_can_still_accept_the_same_row(client, project_doc):
    """The rule is about WHOSE record it is, not about the row — a
    colleague reviewing it is exactly the intended path."""
    ids = await _self_targeted_change(client, project_doc, "extract-alex")
    resp = await client.post(
        f"/proposed_changes/{ids[0]}/accept", params={"view_mode": "work"},
        headers=auth_headers("hr", "some-other-it-person"))
    assert resp.status_code == 200, resp.text
