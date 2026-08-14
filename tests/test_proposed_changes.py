"""Doc upload -> extraction -> review.

The load-bearing assertion in this module is
test_proposed_change_is_not_searchable_until_accepted: extracted content is
inert until a human accepts it. Everything else here exists to make that one
meaningful — that the rows really were created, really were pending, and
really did become live at the moment of acceptance and not before.

Runs entirely on the mock extractor (AI_MODE is unset and no chat deployment
is configured in tests — see conftest), so nothing here touches a model API.
"""
import io
import json

import pytest

from app.models import AuditLog, EmployeeProject, EmployeeSkill, ProposedChange
from app.models.enums import ProposedChangeStatus
from tests.conftest import auth_headers

DOC_TEXT = """Weekly status report

Alex Kim worked on Project Nightingale. Rebuilt the ingest pipeline using Terraform, Python.
Jamie Doubleton worked on Project Atlas. Migrated the ledger tables.
Robin Nobody worked on Project Phantom. Wrote the onboarding docs.
"""


def _docx_bytes(text: str) -> bytes:
    import docx

    document = docx.Document()
    for line in text.splitlines():
        document.add_paragraph(line)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _upload(client, role="it", view_mode="work", text=DOC_TEXT, filename="status.docx"):
    return client.post(
        "/docs/upload", params={"view_mode": view_mode},
        files={"file": (filename, _docx_bytes(text),
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
        headers=auth_headers(role, "it-reviewer-1"),
    )


@pytest.fixture
async def uploaded(client):
    resp = await _upload(client)
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Upload: IT + work mode only, docx/pdf only.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("role", ["employee", "manager", "hr"])
async def test_only_it_can_upload_documents(client, role):
    resp = await _upload(client, role=role)
    assert resp.status_code == 403, resp.text


async def test_it_cannot_upload_in_employee_mode(client):
    resp = await _upload(client, view_mode="employee")
    assert resp.status_code == 403


async def test_unsupported_file_type_is_415(client):
    resp = await client.post(
        "/docs/upload", params={"view_mode": "work"},
        files={"file": ("notes.txt", b"Alex Kim worked on X. Did things.", "text/plain")},
        headers=auth_headers("it"),
    )
    assert resp.status_code == 415


async def test_upload_parses_and_queues(uploaded):
    assert uploaded["characters_extracted"] > 0
    assert uploaded["proposed_changes"] >= 3
    assert uploaded["status"] == "pending"


# ---------------------------------------------------------------------------
# Name resolution: resolve, ambiguous, absent.
# ---------------------------------------------------------------------------

async def test_name_resolution_outcomes(client, db_session, uploaded):
    rows = (
        db_session.query(ProposedChange)
        .filter(ProposedChange.source_doc_id == uploaded["doc_id"])
        .all()
    )
    by_guess = {json.loads(r.raw_extraction)["member_name_guess"]: r for r in rows}

    # Unique name -> resolved.
    assert by_guess["Alex Kim"].employee_id == "extract-alex"
    # Two people share it -> deliberately unresolved, not a coin flip.
    assert by_guess["Jamie Doubleton"].employee_id is None
    # Nobody has it -> unresolved.
    assert by_guess["Robin Nobody"].employee_id is None

    assert uploaded["unresolved"] >= 2


async def test_everything_starts_pending(db_session, uploaded):
    rows = (
        db_session.query(ProposedChange)
        .filter(ProposedChange.source_doc_id == uploaded["doc_id"])
        .all()
    )
    assert rows
    assert all(r.status is ProposedChangeStatus.pending for r in rows)
    assert all(r.reviewed_by is None and r.reviewed_at is None for r in rows)


# ---------------------------------------------------------------------------
# THE guarantee: nothing extracted is live until accepted.
# ---------------------------------------------------------------------------

async def test_proposed_change_is_not_searchable_until_accepted(client, db_session):
    """The load-bearing guarantee, on names unique to this test.

    Deliberately does NOT reuse the shared `uploaded` fixture: the database
    is seeded once per session, so asserting "extract-alex has no projects"
    would really be asserting "no earlier test in this file accepted one",
    which is a statement about test ordering rather than about the
    behaviour. Everything named here appears nowhere else in the suite and
    is never accepted, so the assertions hold whatever order tests run in.
    """
    from app.models import Project

    resp = await _upload(client, text=(
        "Alex Kim worked on Project Unaccepted. Built the widget using Kubernetes.\n"
    ), filename="never-accepted.docx")
    assert resp.status_code == 201
    doc_id = resp.json()["doc_id"]

    # Staged, and staged against a real person — so what follows is about
    # acceptance, not about the extraction having quietly failed.
    rows = db_session.query(ProposedChange).filter(
        ProposedChange.source_doc_id == doc_id).all()
    assert rows and all(r.status is ProposedChangeStatus.pending for r in rows)
    assert any(r.employee_id == "extract-alex" for r in rows)

    # The project does not exist as a row...
    assert db_session.query(Project).filter(Project.name == "Project Unaccepted").first() is None

    # ...nobody is attached to it...
    assert not [
        r for r in db_session.query(EmployeeProject).filter(
            EmployeeProject.employee_id == "extract-alex").all()
        if r.project and r.project.name == "Project Unaccepted"
    ]

    # ...the skill it would have granted was not granted...
    skill_names = {
        r.skill.name for r in db_session.query(EmployeeSkill).filter(
            EmployeeSkill.employee_id == "extract-alex").all() if r.skill
    }
    assert "Kubernetes" not in skill_names

    # ...and it is not retrievable, through either find_people arm.
    for params in ({"name": "Project Unaccepted"}, {"query": "Project Unaccepted"}):
        found = await client.get("/people", params=params, headers=auth_headers("it"))
        assert found.status_code == 200
        assert found.json() == [], f"{params} leaked a pending proposal"


async def test_pending_content_absent_from_profile(client):
    """Same order-independence reasoning as the test above — its own upload,
    its own never-accepted project name."""
    resp = await _upload(client, text=(
        "Alex Kim worked on Project Unprofiled. Wrote the migration scripts.\n"
    ), filename="unprofiled.docx")
    assert resp.status_code == 201

    profile = await client.get(
        "/people/extract-alex", params={"view_mode": "work"}, headers=auth_headers("it"))
    names = [p["project_name"] for p in profile.json()["project_history"]]
    assert "Project Unprofiled" not in names


# ---------------------------------------------------------------------------
# Accept: the one path that commits.
# ---------------------------------------------------------------------------

async def _proposal_ids(client, doc_id, employee_id=None):
    resp = await client.get(
        "/proposed_changes", params={"doc_id": doc_id, "view_mode": "work"},
        headers=auth_headers("it"))
    assert resp.status_code == 200
    out = []
    for group in resp.json()["groups"]:
        if employee_id is not None and group["employee_id"] != employee_id:
            continue
        out.extend((c["id"], c["field_type"]) for c in group["changes"])
    return out


async def test_accept_commits_project_and_reindexes(client, db_session, uploaded):
    ids = await _proposal_ids(client, uploaded["doc_id"], employee_id="extract-alex")
    project_id = next(i for i, kind in ids if kind == "project")

    resp = await client.post(
        f"/proposed_changes/{project_id}/accept", params={"view_mode": "work"},
        headers=auth_headers("it", "it-reviewer-1"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "accepted"

    db_session.expire_all()
    rows = db_session.query(EmployeeProject).filter(
        EmployeeProject.employee_id == "extract-alex").all()
    assert len(rows) == 1
    assert rows[0].contribution and "ingest pipeline" in rows[0].contribution


async def test_accept_writes_audit_row_with_ai_extraction_source(client, db_session, uploaded):
    ids = await _proposal_ids(client, uploaded["doc_id"], employee_id="extract-alex")
    skill_id = next(i for i, kind in ids if kind == "skill")

    resp = await client.post(
        f"/proposed_changes/{skill_id}/accept", params={"view_mode": "work"},
        headers=auth_headers("it", "it-reviewer-1"))
    assert resp.status_code == 200

    row = (
        db_session.query(AuditLog)
        .filter(AuditLog.action == "accept_proposed_change")
        .order_by(AuditLog.id.desc()).first()
    )
    assert row is not None
    assert row.source == "ai_extraction"
    assert row.actor_id == "it-reviewer-1"


async def test_accepted_skill_lands_as_learning_self_reported(client, db_session, uploaded):
    """An uploaded document is evidence somebody touched a thing, not that
    they're an expert in it — otherwise find_mentor starts recommending
    people on the strength of a status report."""
    ids = await _proposal_ids(client, uploaded["doc_id"], employee_id="extract-alex")
    skill_id = next(i for i, kind in ids if kind == "skill")
    await client.post(f"/proposed_changes/{skill_id}/accept", params={"view_mode": "work"},
                      headers=auth_headers("it"))

    db_session.expire_all()
    rows = db_session.query(EmployeeSkill).filter(
        EmployeeSkill.employee_id == "extract-alex").all()
    assert rows
    assert all(r.level.value == "Learning" for r in rows)
    assert all(r.source.value == "self" for r in rows)


async def test_accepting_unresolved_proposal_is_409(client, uploaded):
    """No employee to attach it to — reassign first. Accepting anyway would
    produce a membership belonging to nobody."""
    ids = await _proposal_ids(client, uploaded["doc_id"], employee_id=None)
    unresolved = next(i for i, _ in ids)
    resp = await client.post(
        f"/proposed_changes/{unresolved}/accept", params={"view_mode": "work"},
        headers=auth_headers("it"))
    assert resp.status_code == 409, resp.text


async def test_accept_is_not_repeatable(client, uploaded):
    ids = await _proposal_ids(client, uploaded["doc_id"], employee_id="extract-alex")
    proposal_id = next(i for i, kind in ids if kind == "project")
    first = await client.post(f"/proposed_changes/{proposal_id}/accept",
                              params={"view_mode": "work"}, headers=auth_headers("it"))
    second = await client.post(f"/proposed_changes/{proposal_id}/accept",
                               params={"view_mode": "work"}, headers=auth_headers("it"))
    assert first.status_code == 200
    assert second.status_code == 409


# ---------------------------------------------------------------------------
# Reassign / correct: both stay pending.
# ---------------------------------------------------------------------------

async def test_reassign_resolves_and_stays_pending(client, db_session, uploaded):
    ids = await _proposal_ids(client, uploaded["doc_id"], employee_id=None)
    proposal_id = next(i for i, kind in ids if kind == "project")

    resp = await client.post(
        f"/proposed_changes/{proposal_id}/reassign", params={"view_mode": "work"},
        json={"employee_id": "extract-dup-1"}, headers=auth_headers("it"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["employee_id"] == "extract-dup-1"
    assert resp.json()["status"] == "pending", "reassigning says who, not that it's true"

    db_session.expire_all()
    row = db_session.get(ProposedChange, proposal_id)
    assert row.status is ProposedChangeStatus.pending
    # raw_extraction is never rewritten — what the model read stays on record.
    assert json.loads(row.raw_extraction)["member_name_guess"] in ("Jamie Doubleton", "Robin Nobody")


async def test_reassign_then_accept_commits_to_the_new_person(client, db_session, uploaded):
    ids = await _proposal_ids(client, uploaded["doc_id"], employee_id=None)
    proposal_id = next(i for i, kind in ids if kind == "project")

    await client.post(f"/proposed_changes/{proposal_id}/reassign", params={"view_mode": "work"},
                      json={"employee_id": "extract-dup-2"}, headers=auth_headers("it"))
    resp = await client.post(f"/proposed_changes/{proposal_id}/accept",
                             params={"view_mode": "work"}, headers=auth_headers("it"))
    assert resp.status_code == 200
    # A human changed the content, so it records as `edited`, not `accepted`.
    assert resp.json()["status"] == "edited"

    db_session.expire_all()
    assert db_session.query(EmployeeProject).filter(
        EmployeeProject.employee_id == "extract-dup-2").count() == 1


async def test_correct_stays_pending_and_updates_content(client, db_session, uploaded):
    ids = await _proposal_ids(client, uploaded["doc_id"], employee_id="extract-alex")
    proposal_id = next(i for i, kind in ids if kind == "project")

    resp = await client.post(
        f"/proposed_changes/{proposal_id}/correct", params={"view_mode": "work"},
        json={"instruction": "project: Project Nightingale Phase 2"},
        headers=auth_headers("it"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "pending"
    assert resp.json()["proposed_content"]["project"] == "Project Nightingale Phase 2"

    db_session.expire_all()
    assert db_session.get(ProposedChange, proposal_id).status is ProposedChangeStatus.pending


async def test_reassign_to_unknown_employee_is_409(client, uploaded):
    ids = await _proposal_ids(client, uploaded["doc_id"], employee_id=None)
    proposal_id = next(i for i, _ in ids)
    resp = await client.post(
        f"/proposed_changes/{proposal_id}/reassign", params={"view_mode": "work"},
        json={"employee_id": "nobody-at-all"}, headers=auth_headers("it"))
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Reject.
# ---------------------------------------------------------------------------

async def test_reject_marks_rejected_and_commits_nothing(client, db_session, uploaded):
    ids = await _proposal_ids(client, uploaded["doc_id"], employee_id="extract-alex")
    proposal_id = next(i for i, kind in ids if kind == "project")

    # A delta, not an absolute count: the database is seeded once per test
    # session (see conftest), so an earlier test in this module has legitimately
    # accepted a project for this person already. What this test asserts is
    # that *rejecting* commits nothing — not that the table is empty.
    before = db_session.query(EmployeeProject).filter(
        EmployeeProject.employee_id == "extract-alex").count()

    resp = await client.delete(
        f"/proposed_changes/{proposal_id}", params={"view_mode": "work"},
        headers=auth_headers("it", "it-reviewer-9"))
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"

    db_session.expire_all()
    row = db_session.get(ProposedChange, proposal_id)
    # Kept, not deleted — a rejected proposal is the most useful row in the
    # table when extraction quality is next reviewed.
    assert row is not None
    assert row.reviewed_by == "it-reviewer-9"
    after = db_session.query(EmployeeProject).filter(
        EmployeeProject.employee_id == "extract-alex").count()
    assert after == before


async def test_rejected_proposal_cannot_then_be_accepted(client, uploaded):
    ids = await _proposal_ids(client, uploaded["doc_id"], employee_id="extract-alex")
    proposal_id = next(i for i, kind in ids if kind == "project")
    await client.delete(f"/proposed_changes/{proposal_id}", params={"view_mode": "work"},
                        headers=auth_headers("it"))
    resp = await client.post(f"/proposed_changes/{proposal_id}/accept",
                             params={"view_mode": "work"}, headers=auth_headers("it"))
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Review endpoints are IT + work mode, called directly.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("role", ["employee", "manager", "hr"])
async def test_review_queue_is_it_only(client, role, uploaded):
    resp = await client.get(
        "/proposed_changes", params={"doc_id": uploaded["doc_id"], "view_mode": "work"},
        headers=auth_headers(role))
    assert resp.status_code == 403


async def test_review_queue_denied_to_it_in_employee_mode(client, uploaded):
    resp = await client.get(
        "/proposed_changes", params={"doc_id": uploaded["doc_id"], "view_mode": "employee"},
        headers=auth_headers("it"))
    assert resp.status_code == 403


@pytest.mark.parametrize("role", ["employee", "manager", "hr"])
async def test_accept_is_it_only(client, uploaded, role):
    ids = await _proposal_ids(client, uploaded["doc_id"], employee_id="extract-alex")
    proposal_id = next(i for i, _ in ids)
    resp = await client.post(
        f"/proposed_changes/{proposal_id}/accept", params={"view_mode": "work"},
        headers=auth_headers(role))
    assert resp.status_code == 403


async def test_unresolved_groups_sort_first(client, uploaded):
    resp = await client.get(
        "/proposed_changes", params={"doc_id": uploaded["doc_id"], "view_mode": "work"},
        headers=auth_headers("it"))
    groups = resp.json()["groups"]
    assert groups[0]["unresolved"] is True, "unresolved rows must not be buried"
