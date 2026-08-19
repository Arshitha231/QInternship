"""IT, work mode: editing anyone's project history EXCEPT their own.

Two rules, tested independently because they fail independently:

  1. IT can create, correct and remove any employee's EmployeeProject row
     directly — without a document, which is the only route that existed
     before (app/proposals.py's accept/edit committing a proposed_change).
  2. IT cannot do any of it to themselves, through either route. The direct
     endpoints refuse it, and so does the review pipeline — which had no
     such check at all, so an IT reviewer could upload a document about
     themselves and accept it onto their own profile.

Every denial below calls the endpoint directly with whatever role and id it
likes: there is no preceding read to have been filtered and no frontend to
have hidden a button, same standard as tests/test_write_endpoints.py.
"""
from datetime import date

import pytest

from app.models import AuditLog, Employee, EmployeeProject, Office, OrgUnit, Project
from app.models.enums import AvailabilityStatus, EmploymentType
from tests.conftest import auth_headers

ALL_ROLES = ["employee", "manager", "hr", "it"]


@pytest.fixture
def atlas_id(db_session):
    return db_session.query(Project).filter(Project.name == "Project Atlas").one().id


def _mkemp(db_session, id_, full_name, **overrides) -> Employee:
    """Throwaway employee per test — never a shared conftest fixture
    person, since the test database is session-scoped."""
    office = db_session.query(Office).filter(Office.name == "Test HQ").one()
    org_unit = db_session.query(OrgUnit).filter(OrgUnit.name == "Platform Engineering").one()
    fields = dict(
        id=id_, full_name=full_name, job_title="Test Employee",
        org_unit_id=org_unit.id, office_id=office.id, manager_id=None,
        work_email=f"{id_}@example.test", employment_type=EmploymentType.fte,
        hire_date=date(2021, 1, 1), availability_status=AvailabilityStatus.available,
        is_active=True,
    )
    fields.update(overrides)
    emp = Employee(**fields)
    db_session.add(emp)
    db_session.commit()
    return emp


def _membership(db_session, employee_id, project_id) -> EmployeeProject | None:
    db_session.expire_all()
    return (
        db_session.query(EmployeeProject)
        .filter(EmployeeProject.employee_id == employee_id,
                EmployeeProject.project_id == project_id)
        .first()
    )


# ---------------------------------------------------------------------------
# The new capability: edit anyone's project history.
# ---------------------------------------------------------------------------

async def test_it_creates_a_project_membership(client, db_session, atlas_id):
    _mkemp(db_session, "ph-create-1", "Pat Create")
    resp = await client.put(
        f"/people/ph-create-1/projects/{atlas_id}", params={"view_mode": "work"},
        json={"role": "Platform Lead", "start_date": "2025-02-01",
              "contribution": "Owned the cutover plan."},
        headers=auth_headers("it", "it-writer-1"),
    )
    assert resp.status_code == 200, resp.text

    row = _membership(db_session, "ph-create-1", atlas_id)
    assert row is not None
    assert row.role == "Platform Lead"
    assert row.start_date == date(2025, 2, 1)
    assert row.contribution == "Owned the cutover plan."
    assert row.end_date is None


async def test_it_patches_only_the_supplied_keys(client, db_session, atlas_id):
    """PATCH semantics on an existing row, same contract as
    update_employee: an omitted key is untouched, not reset."""
    _mkemp(db_session, "ph-patch-1", "Pat Patch")
    await client.put(
        f"/people/ph-patch-1/projects/{atlas_id}", params={"view_mode": "work"},
        json={"role": "Engineer", "start_date": "2024-01-01",
              "contribution": "Original prose."},
        headers=auth_headers("it"),
    )
    resp = await client.put(
        f"/people/ph-patch-1/projects/{atlas_id}", params={"view_mode": "work"},
        json={"role": "Senior Engineer"}, headers=auth_headers("it"),
    )
    assert resp.status_code == 200, resp.text

    row = _membership(db_session, "ph-patch-1", atlas_id)
    assert row.role == "Senior Engineer"
    assert row.contribution == "Original prose."      # untouched
    assert row.start_date == date(2024, 1, 1)          # untouched


async def test_explicit_null_end_date_makes_a_project_current_again(
    client, db_session, atlas_id
):
    """The distinction the PATCH-with-partial-dict contract exists for:
    null clears, omission leaves alone."""
    _mkemp(db_session, "ph-null-1", "Pat Null")
    await client.put(
        f"/people/ph-null-1/projects/{atlas_id}", params={"view_mode": "work"},
        json={"role": "Engineer", "start_date": "2024-01-01", "end_date": "2024-09-01"},
        headers=auth_headers("it"),
    )
    assert _membership(db_session, "ph-null-1", atlas_id).end_date == date(2024, 9, 1)

    resp = await client.put(
        f"/people/ph-null-1/projects/{atlas_id}", params={"view_mode": "work"},
        json={"end_date": None}, headers=auth_headers("it"),
    )
    assert resp.status_code == 200, resp.text
    assert _membership(db_session, "ph-null-1", atlas_id).end_date is None


async def test_repeating_the_put_converges_on_one_row(client, db_session, atlas_id):
    """(person, project) identifies the membership, so PUT is idempotent —
    it must never stack duplicate rows for the same pair."""
    _mkemp(db_session, "ph-idem-1", "Pat Idem")
    for _ in range(3):
        await client.put(
            f"/people/ph-idem-1/projects/{atlas_id}", params={"view_mode": "work"},
            json={"role": "Engineer", "start_date": "2024-01-01"},
            headers=auth_headers("it"),
        )
    db_session.expire_all()
    rows = (
        db_session.query(EmployeeProject)
        .filter(EmployeeProject.employee_id == "ph-idem-1",
                EmployeeProject.project_id == atlas_id).all()
    )
    assert len(rows) == 1


async def test_it_removes_a_project_membership(client, db_session, atlas_id):
    _mkemp(db_session, "ph-del-1", "Pat Delete")
    await client.put(
        f"/people/ph-del-1/projects/{atlas_id}", params={"view_mode": "work"},
        json={"role": "Engineer", "start_date": "2024-01-01"}, headers=auth_headers("it"),
    )
    assert _membership(db_session, "ph-del-1", atlas_id) is not None

    resp = await client.delete(
        f"/people/ph-del-1/projects/{atlas_id}", params={"view_mode": "work"},
        headers=auth_headers("it"),
    )
    assert resp.status_code == 204, resp.text
    assert _membership(db_session, "ph-del-1", atlas_id) is None

    # The project itself survives — this removes a membership, not a project.
    assert db_session.get(Project, atlas_id) is not None


async def test_creating_requires_role_and_start_date(client, db_session, atlas_id):
    """Both are NOT NULL on EmployeeProject and there's no document here to
    default them from, so a create that omits them is a 422, not a row with
    invented values."""
    _mkemp(db_session, "ph-req-1", "Pat Required")
    resp = await client.put(
        f"/people/ph-req-1/projects/{atlas_id}", params={"view_mode": "work"},
        json={"contribution": "prose only"}, headers=auth_headers("it"),
    )
    assert resp.status_code == 422, resp.text
    assert _membership(db_session, "ph-req-1", atlas_id) is None


async def test_end_date_before_start_date_is_refused(client, db_session, atlas_id):
    _mkemp(db_session, "ph-order-1", "Pat Order")
    resp = await client.put(
        f"/people/ph-order-1/projects/{atlas_id}", params={"view_mode": "work"},
        json={"role": "Engineer", "start_date": "2024-06-01", "end_date": "2024-01-01"},
        headers=auth_headers("it"),
    )
    assert resp.status_code == 422, resp.text


async def test_unknown_employee_or_project_is_404(client, db_session, atlas_id):
    _mkemp(db_session, "ph-404-1", "Pat Missing")
    missing_person = await client.put(
        f"/people/nobody-at-all/projects/{atlas_id}", params={"view_mode": "work"},
        json={"role": "Engineer", "start_date": "2024-01-01"}, headers=auth_headers("it"),
    )
    assert missing_person.status_code == 404

    missing_project = await client.put(
        "/people/ph-404-1/projects/999999", params={"view_mode": "work"},
        json={"role": "Engineer", "start_date": "2024-01-01"}, headers=auth_headers("it"),
    )
    assert missing_project.status_code == 404


async def test_write_is_audited(client, db_session, atlas_id):
    _mkemp(db_session, "ph-audit-1", "Pat Audit")
    await client.put(
        f"/people/ph-audit-1/projects/{atlas_id}", params={"view_mode": "work"},
        json={"role": "Engineer", "start_date": "2024-01-01"},
        headers=auth_headers("it", "it-auditor-1"),
    )
    rows = (
        db_session.query(AuditLog)
        .filter(AuditLog.actor_id == "it-auditor-1",
                AuditLog.action == "create_project_history").all()
    )
    assert len(rows) == 1
    assert "ph-audit-1" in rows[0].query_text


# ---------------------------------------------------------------------------
# The exclusion: except their own.
# ---------------------------------------------------------------------------

async def test_it_cannot_edit_their_own_project_history(client, db_session, atlas_id):
    """The rule the whole feature is scoped by. Same shape as
    update_employee's "an hr caller giving themselves a raise"."""
    _mkemp(db_session, "ph-self-1", "Pat Self")
    resp = await client.put(
        f"/people/ph-self-1/projects/{atlas_id}", params={"view_mode": "work"},
        json={"role": "Principal Engineer", "start_date": "2024-01-01"},
        headers=auth_headers("it", "ph-self-1"),
    )
    assert resp.status_code == 403, resp.text
    assert _membership(db_session, "ph-self-1", atlas_id) is None


async def test_it_cannot_remove_their_own_project_history(client, db_session, atlas_id):
    _mkemp(db_session, "ph-self-2", "Pat Self Two")
    await client.put(
        f"/people/ph-self-2/projects/{atlas_id}", params={"view_mode": "work"},
        json={"role": "Engineer", "start_date": "2024-01-01"},
        headers=auth_headers("it", "it-someone-else"),
    )
    resp = await client.delete(
        f"/people/ph-self-2/projects/{atlas_id}", params={"view_mode": "work"},
        headers=auth_headers("it", "ph-self-2"),
    )
    assert resp.status_code == 403, resp.text
    assert _membership(db_session, "ph-self-2", atlas_id) is not None


@pytest.mark.parametrize("role", [r for r in ALL_ROLES if r != "it"])
async def test_only_it_may_edit_project_history(client, db_session, atlas_id, role):
    _mkemp(db_session, f"ph-role-{role}", f"Pat {role}")
    resp = await client.put(
        f"/people/ph-role-{role}/projects/{atlas_id}", params={"view_mode": "work"},
        json={"role": "Engineer", "start_date": "2024-01-01"}, headers=auth_headers(role),
    )
    assert resp.status_code == 403, resp.text


async def test_it_cannot_edit_project_history_in_employee_mode(
    client, db_session, atlas_id
):
    """EDITABLE[("it", "employee")] is empty — the capability is work-mode
    only, and asking for employee mode must not be a way around that."""
    _mkemp(db_session, "ph-mode-1", "Pat Mode")
    resp = await client.put(
        f"/people/ph-mode-1/projects/{atlas_id}", params={"view_mode": "employee"},
        json={"role": "Engineer", "start_date": "2024-01-01"}, headers=auth_headers("it"),
    )
    assert resp.status_code == 403, resp.text


async def test_identifying_columns_are_not_editable(client, db_session, atlas_id):
    """employee_id/project_id identify the row rather than describe it —
    moving a membership is a delete plus a create, never a field edit."""
    _mkemp(db_session, "ph-immutable-1", "Pat Immutable")
    resp = await client.put(
        f"/people/ph-immutable-1/projects/{atlas_id}", params={"view_mode": "work"},
        json={"role": "Engineer", "start_date": "2024-01-01",
              "employee_id": "somebody-else"},
        headers=auth_headers("it"),
    )
    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# The read side has to name the row the write side addresses.
# ---------------------------------------------------------------------------

async def test_project_history_exposes_the_id_the_write_path_needs(
    client, db_session, atlas_id
):
    """ProfilePage edits a membership by (person, project), so the id has
    to travel with the row it edits — without it the UI can render project
    history it has no way to address."""
    _mkemp(db_session, "ph-readid-1", "Pat ReadId")
    await client.put(
        f"/people/ph-readid-1/projects/{atlas_id}", params={"view_mode": "work"},
        json={"role": "Engineer", "start_date": "2024-01-01"}, headers=auth_headers("it"),
    )
    resp = await client.get(
        "/people/ph-readid-1", params={"view_mode": "work"}, headers=auth_headers("it"))
    assert resp.status_code == 200, resp.text

    history = resp.json()["project_history"]
    row = next(p for p in history if p["project_id"] == atlas_id)
    assert row["project_name"] == "Project Atlas"
    # Round-trips: the id the read handed back addresses the same row.
    delete = await client.delete(
        f"/people/ph-readid-1/projects/{row['project_id']}", params={"view_mode": "work"},
        headers=auth_headers("it"))
    assert delete.status_code == 204
