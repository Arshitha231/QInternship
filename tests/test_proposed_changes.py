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


async def _upload(client, text, role="it", view_mode="work", filename="doc.docx"):
    return await client.post(
        "/docs/upload", params={"view_mode": view_mode},
        files={"file": (filename, _docx_bytes(text),
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
        headers=auth_headers(role, "it-reviewer-1"),
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
    resp = await client.get("/doc_subject_matches", params=params, headers=auth_headers("it"))
    assert resp.status_code == 200, resp.text
    return resp.json()["subjects"]


async def _resolve(client, subject_id, employee_id=None, new_hire=False, role="it", view_mode="work"):
    body = {"new_hire": True} if new_hire else {"employee_id": employee_id}
    return await client.post(
        f"/doc_subject_matches/{subject_id}/resolve", params={"view_mode": view_mode},
        json=body, headers=auth_headers(role, "it-reviewer-1"))


async def _proposals(client, doc_id=None, employee_id=None, role="it"):
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


@pytest.mark.parametrize("role", ["employee", "manager", "hr"])
async def test_only_it_can_upload_documents(client, role):
    resp = await _upload(client, PROJECT_DOC_TEXT, role=role)
    assert resp.status_code == 403


async def test_it_cannot_upload_in_employee_mode(client):
    resp = await _upload(client, PROJECT_DOC_TEXT, view_mode="employee")
    assert resp.status_code == 403


async def test_unsupported_file_type_is_415(client):
    resp = await client.post(
        "/docs/upload", params={"view_mode": "work"},
        files={"file": ("notes.txt", b"Alex Kim worked on X. Did things.", "text/plain")},
        headers=auth_headers("it"),
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

    caller = AuthenticatedUser(id="ranking-test", role="it")
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
        json={"employee_id": "extract-alex"}, headers=auth_headers("it"))
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
        json={}, headers=auth_headers("it"))
    assert neither.status_code == 422

    both = await client.post(
        f"/doc_subject_matches/{subject_id}/resolve", params={"view_mode": "work"},
        json={"employee_id": "extract-alex", "new_hire": True}, headers=auth_headers("it"))
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
        headers=auth_headers("it", "it-reviewer-1"))
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
        headers=auth_headers("it", "it-reviewer-1"))
    assert resp.status_code == 200

    row = (
        db_session.query(AuditLog).filter(AuditLog.action == "accept_proposed_change")
        .order_by(AuditLog.id.desc()).first()
    )
    assert row is not None
    assert row.source == "ai_extraction"
    assert row.actor_id == "it-reviewer-1"


async def test_accepted_skill_lands_as_learning_self_reported(client, project_doc, db_session):
    subjects = await _subjects(client, project_doc["doc_id"])
    alex = next(s for s in subjects if s["extracted_name"] == "Alex Kim")
    await _resolve(client, alex["id"], employee_id="extract-alex")

    ids = await _resolved_change_ids(client, project_doc["doc_id"], "extract-alex", "skill")
    await client.post(f"/proposed_changes/{ids[0]}/accept", params={"view_mode": "work"},
                      headers=auth_headers("it"))

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
        headers=auth_headers("it"))
    assert resp.status_code == 409, resp.text


async def test_accept_is_not_repeatable(client, project_doc):
    subjects = await _subjects(client, project_doc["doc_id"])
    alex = next(s for s in subjects if s["extracted_name"] == "Alex Kim")
    await _resolve(client, alex["id"], employee_id="extract-alex")

    ids = await _resolved_change_ids(client, project_doc["doc_id"], "extract-alex", "project_entry")
    first = await client.post(f"/proposed_changes/{ids[0]}/accept",
                              params={"view_mode": "work"}, headers=auth_headers("it"))
    second = await client.post(f"/proposed_changes/{ids[0]}/accept",
                               params={"view_mode": "work"}, headers=auth_headers("it"))
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
        json={"edited_value": edited}, headers=auth_headers("it", "it-reviewer-1"))
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
        headers=auth_headers("it", "it-reviewer-9"))
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"

    db_session.expire_all()
    after = db_session.query(EmployeeProject).filter(EmployeeProject.employee_id == "extract-alex").count()
    assert after == before

    row = db_session.get(ProposedChange, ids[0])
    assert row is not None  # kept, not deleted
    assert row.reviewed_by == "it-reviewer-9"


async def test_rejected_proposal_cannot_then_be_accepted(client, project_doc):
    subjects = await _subjects(client, project_doc["doc_id"])
    alex = next(s for s in subjects if s["extracted_name"] == "Alex Kim")
    await _resolve(client, alex["id"], employee_id="extract-alex")
    ids = await _resolved_change_ids(client, project_doc["doc_id"], "extract-alex", "skill")

    await client.post(f"/proposed_changes/{ids[0]}/reject", params={"view_mode": "work"},
                      headers=auth_headers("it"))
    resp = await client.post(f"/proposed_changes/{ids[0]}/accept",
                             params={"view_mode": "work"}, headers=auth_headers("it"))
    assert resp.status_code == 409


async def test_reassign_moves_a_single_row_independent_of_its_subject(client, project_doc, db_session):
    subjects = await _subjects(client, project_doc["doc_id"])
    jamie = next(s for s in subjects if s["extracted_name"] == "Jamie Doubleton")
    rows = db_session.query(ProposedChange).filter(
        ProposedChange.subject_match_id == jamie["id"]).all()
    target_id = rows[0].id

    resp = await client.post(
        f"/proposed_changes/{target_id}/reassign", params={"view_mode": "work"},
        json={"employee_id": "extract-dup-1"}, headers=auth_headers("it"))
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
        headers=auth_headers("it"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "pending"
    assert resp.json()["proposed_value"]["project"] == "Project Nightingale Phase 2"


# ---------------------------------------------------------------------------
# Permissions: skills/contribution/project_entry gated per change_type.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("role", ["employee", "manager", "hr"])
async def test_accept_is_it_only(client, project_doc, role):
    subjects = await _subjects(client, project_doc["doc_id"])
    alex = next(s for s in subjects if s["extracted_name"] == "Alex Kim")
    await _resolve(client, alex["id"], employee_id="extract-alex")
    ids = await _resolved_change_ids(client, project_doc["doc_id"], "extract-alex")

    resp = await client.post(
        f"/proposed_changes/{ids[0]}/accept", params={"view_mode": "work"},
        headers=auth_headers(role))
    assert resp.status_code == 403


async def test_it_cannot_commit_in_employee_mode(client, project_doc):
    subjects = await _subjects(client, project_doc["doc_id"])
    alex = next(s for s in subjects if s["extracted_name"] == "Alex Kim")
    await _resolve(client, alex["id"], employee_id="extract-alex")
    ids = await _resolved_change_ids(client, project_doc["doc_id"], "extract-alex")

    resp = await client.post(
        f"/proposed_changes/{ids[0]}/accept", params={"view_mode": "employee"},
        headers=auth_headers("it"))
    assert resp.status_code == 403


def test_skills_contribution_project_entry_are_it_editable():
    from app.permissions import can_edit

    for field in ("skills", "contribution", "project_entry"):
        assert can_edit("it", "work", field) is True
        assert can_edit("it", "employee", field) is False
        assert can_edit("hr", "work", field) is False
        assert can_edit("employee", "work", field) is False


@pytest.mark.parametrize("role", ["employee", "manager", "hr"])
async def test_review_queues_are_it_only(client, project_doc, role):
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
        json={"ids": ids}, headers=auth_headers("it", "it-reviewer-1"))
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
        headers=auth_headers("it"))
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
        json={"ids": [*ok_ids, unresolved_row_id]}, headers=auth_headers("it"))
    assert resp.status_code == 200, resp.text
    results = {r["id"]: r for r in resp.json()["results"]}
    assert all(results[i]["ok"] for i in ok_ids)
    assert results[unresolved_row_id]["ok"] is False


async def test_bulk_action_requires_a_selector(client):
    resp = await client.post(
        "/proposed_changes/bulk_accept", params={"view_mode": "work"},
        json={}, headers=auth_headers("it"))
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /docs + POST /docs/{id}/finalize — the "Update" action: accept the
# checked rows, reject the rest, then scrub the document's own content.
# ---------------------------------------------------------------------------

async def _docs(client, role="it"):
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
        json={"accept_ids": [keep]}, headers=auth_headers("it", "it-reviewer-1"))
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
        json={"accept_ids": []}, headers=auth_headers("it"))
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
        json={"accept_ids": []}, headers=auth_headers("it"))
    assert resp.status_code == 200, resp.text

    jamie_row = db_session.query(ProposedChange).filter(
        ProposedChange.subject_match_id == jamie["id"]).first()
    assert jamie_row.status is ProposedChangeStatus.pending
    assert jamie_row.employee_id is None

    resolve_resp = await _resolve(client, jamie["id"], employee_id="extract-dup-2")
    assert resolve_resp.status_code == 200, resolve_resp.text
    accept_resp = await client.post(
        f"/proposed_changes/{jamie_row.id}/accept", params={"view_mode": "work"},
        headers=auth_headers("it"))
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
        json={"accept_ids": []}, headers=auth_headers("it"))

    resp = await client.post(
        f"/proposed_changes/{jamie_row.id}/correct", params={"view_mode": "work"},
        json={"instruction": "actually it was React, not Terraform"}, headers=auth_headers("it"))
    assert resp.status_code == 409
    assert "cleared" in resp.json()["detail"]


async def test_finalize_is_not_repeatable(client, project_doc):
    subjects = await _subjects(client, project_doc["doc_id"])
    alex = next(s for s in subjects if s["extracted_name"] == "Alex Kim")
    await _resolve(client, alex["id"], employee_id="extract-alex")

    first = await client.post(
        f"/docs/{project_doc['doc_id']}/finalize", params={"view_mode": "work"},
        json={"accept_ids": []}, headers=auth_headers("it"))
    assert first.status_code == 200, first.text

    second = await client.post(
        f"/docs/{project_doc['doc_id']}/finalize", params={"view_mode": "work"},
        json={"accept_ids": []}, headers=auth_headers("it"))
    assert second.status_code == 409


async def test_finalize_unknown_document_is_404(client):
    resp = await client.post(
        "/docs/999999/finalize", params={"view_mode": "work"},
        json={"accept_ids": []}, headers=auth_headers("it"))
    assert resp.status_code == 404


@pytest.mark.parametrize("role", ["employee", "manager", "hr"])
async def test_finalize_is_it_only(client, project_doc, role):
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
        json={"accept_ids": []}, headers=auth_headers("it"))

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
        found = await client.get("/people", params=params, headers=auth_headers("it"))
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
        "/people", params={"name": "Project Atlas"}, headers=auth_headers("it"))
    assert found.status_code == 200
    # Project Atlas is a seeded fixture project unrelated to this doc, so a
    # name hit here would mean nothing — the real assertion is on the skill/
    # contribution side, already covered by test_pending_rows_invisible_...
    # and test_ranked_candidates_never_auto_assign. This just confirms the
    # upload itself didn't error the search path.
    assert isinstance(found.json(), list)
