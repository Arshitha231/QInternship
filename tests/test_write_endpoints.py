"""Write endpoints: HR internal-field edits and IT project descriptions.

The point of these tests is that authorization is enforced on the WRITE
itself. Every denial case below calls the endpoint directly with whatever
role and view_mode it likes — there is no preceding read to have been
filtered, and no frontend to have hidden a button. If the only thing
stopping an employee from setting their own salary were a hidden form, all
of these would pass anyway.
"""
import pytest

from app.models import AuditLog, Employee, Project
from tests.conftest import auth_headers

ALL_ROLES = ["employee", "manager", "hr", "it"]


@pytest.fixture
def atlas_id(db_session):
    return db_session.query(Project).filter(Project.name == "Project Atlas").one().id


# ---------------------------------------------------------------------------
# HR, work mode: the permitted case.
# ---------------------------------------------------------------------------

async def test_hr_can_edit_internal_fields_in_work_mode(client, db_session):
    resp = await client.patch(
        "/employees/mgr-1", params={"view_mode": "work"},
        json={"salary": "123456.00", "job_title": "Director of Engineering"},
        headers=auth_headers("hr", "hr-writer-1"),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["salary"] == "123456.00"

    db_session.expire_all()
    row = db_session.get(Employee, "mgr-1")
    assert str(row.salary) == "123456.00"
    assert row.job_title == "Director of Engineering"


async def test_hr_edit_writes_an_audit_row(client, db_session):
    before = db_session.query(AuditLog).count()
    resp = await client.patch(
        "/employees/report-1", params={"view_mode": "work"},
        json={"cost_centre": "CC-ENG-99"},
        headers=auth_headers("hr", "hr-auditor-1"),
    )
    assert resp.status_code == 200

    assert db_session.query(AuditLog).count() > before
    # Not simply "the newest row": the route re-reads the person through the
    # ordinary permission-filtered path afterwards, and that read is itself
    # audited (correctly — it returned fields to a caller). The write's own
    # row is the one being asserted on here.
    row = (
        db_session.query(AuditLog)
        .filter(AuditLog.action == "update_employee")
        .order_by(AuditLog.id.desc()).first()
    )
    assert row is not None
    assert row.actor_id == "hr-auditor-1"
    assert "cost_centre" in row.fields_returned
    # Not an AI-sourced change — provenance stays null rather than being
    # invented. See app/models/audit_log.py.
    assert row.source is None


async def test_patch_with_unknown_field_is_422(client):
    resp = await client.patch(
        "/employees/mgr-1", params={"view_mode": "work"},
        json={"is_active": False},  # real column, deliberately not editable
        headers=auth_headers("hr"),
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# HR, work mode: the denied cases. Called directly, no UI involved.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("role", ["employee", "manager", "it"])
async def test_non_hr_cannot_edit_internal_fields(client, db_session, role):
    resp = await client.patch(
        "/employees/mgr-1", params={"view_mode": "work"},
        json={"salary": "999999.00"},
        headers=auth_headers(role),
    )
    assert resp.status_code == 403, resp.text

    db_session.expire_all()
    assert str(db_session.get(Employee, "mgr-1").salary) != "999999.00"


async def test_hr_cannot_edit_in_employee_mode(client, db_session):
    """view_mode is enforced on the write, not just the read that precedes
    it — HR in employee mode is an ordinary employee, including here."""
    resp = await client.patch(
        "/employees/mgr-1", params={"view_mode": "employee"},
        json={"salary": "888888.00"},
        headers=auth_headers("hr"),
    )
    assert resp.status_code == 403, resp.text

    db_session.expire_all()
    assert str(db_session.get(Employee, "mgr-1").salary) != "888888.00"


async def test_hr_cannot_edit_own_profile(client, db_session):
    """HR may edit every profile except their own — the admin edit path is
    not a self-service one, even for the role that otherwise has full
    write access through it. mgr-1 is HR's own id here specifically (not
    a bystander's), so this is a self-edit attempt, not a permission gap."""
    resp = await client.patch(
        "/employees/mgr-1", params={"view_mode": "work"},
        json={"salary": "555555.00"}, headers=auth_headers("hr", "mgr-1"),
    )
    assert resp.status_code == 403, resp.text

    db_session.expire_all()
    assert str(db_session.get(Employee, "mgr-1").salary) != "555555.00"


async def test_hr_can_still_edit_someone_elses_profile(client, db_session):
    """The self-block is scoped to the caller's own id, not a blanket
    regression on HR's write access to everyone else."""
    resp = await client.patch(
        "/employees/report-1", params={"view_mode": "work"},
        json={"job_title": "Staff Engineer"}, headers=auth_headers("hr", "mgr-1"),
    )
    assert resp.status_code == 200, resp.text

    db_session.expire_all()
    assert db_session.get(Employee, "report-1").job_title == "Staff Engineer"


async def test_hr_self_edit_blocked_before_target_lookup(client):
    """Self-block fires even when the caller's own id has no employee row
    (a dev-mode caller id is just a header value, not guaranteed to exist)
    — the check is about identity, not about what get() would return, and
    must not depend on the row existing to catch the attempt."""
    resp = await client.patch(
        "/employees/no-such-employee-id", params={"view_mode": "work"},
        json={"salary": "1.00"}, headers=auth_headers("hr", "no-such-employee-id"),
    )
    assert resp.status_code == 403, resp.text


async def test_employee_cannot_set_own_salary(client, db_session):
    """The obvious attack: ABAC grants a caller sight of their own salary,
    which must not imply the ability to change it."""
    resp = await client.patch(
        "/employees/stranger-1", params={"view_mode": "work"},
        json={"salary": "1000000.00"},
        headers=auth_headers("employee", "stranger-1"),
    )
    assert resp.status_code == 403

    db_session.expire_all()
    assert str(db_session.get(Employee, "stranger-1").salary) == "95000.00"


async def test_denied_write_leaves_no_audit_row(client, db_session):
    """A refused write changed nothing, so it must not look like a change
    in the audit trail. (The read pipeline's own audit rows are a separate
    story — this asserts specifically that update_employee didn't log.)"""
    before = db_session.query(AuditLog).filter(AuditLog.action == "update_employee").count()
    await client.patch(
        "/employees/mgr-1", params={"view_mode": "work"},
        json={"salary": "777777.00"}, headers=auth_headers("employee"),
    )
    after = db_session.query(AuditLog).filter(AuditLog.action == "update_employee").count()
    assert after == before


# ---------------------------------------------------------------------------
# IT, work mode: project descriptions.
# ---------------------------------------------------------------------------

async def test_it_can_set_project_description(client, db_session, atlas_id):
    resp = await client.put(
        f"/projects/{atlas_id}/description", params={"view_mode": "work"},
        json={"description": "Rewritten by IT."},
        headers=auth_headers("it", "it-writer-1"),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["project_desc"] == "Rewritten by IT."

    db_session.expire_all()
    assert db_session.get(Project, atlas_id).description == "Rewritten by IT."


async def test_it_can_clear_project_description(client, db_session, atlas_id):
    await client.put(
        f"/projects/{atlas_id}/description", params={"view_mode": "work"},
        json={"description": "temporary"}, headers=auth_headers("it"),
    )
    resp = await client.delete(
        f"/projects/{atlas_id}/description", params={"view_mode": "work"},
        headers=auth_headers("it"),
    )
    assert resp.status_code == 200

    db_session.expire_all()
    assert db_session.get(Project, atlas_id).description is None

    # Restore, so ordering between test modules can't matter.
    await client.put(
        f"/projects/{atlas_id}/description", params={"view_mode": "work"},
        json={"description": "Internal migration of the billing ledger to the new platform."},
        headers=auth_headers("it"),
    )


@pytest.mark.parametrize("role", ["employee", "manager", "hr"])
async def test_only_it_can_edit_project_descriptions(client, db_session, atlas_id, role):
    """HR is included deliberately: HR is the more privileged role for pay
    data and still may not touch a project description. Privilege in this
    system is a table, not a ladder."""
    original = db_session.get(Project, atlas_id).description
    resp = await client.put(
        f"/projects/{atlas_id}/description", params={"view_mode": "work"},
        json={"description": f"written by {role}"}, headers=auth_headers(role),
    )
    assert resp.status_code == 403, resp.text

    db_session.expire_all()
    assert db_session.get(Project, atlas_id).description == original


async def test_it_cannot_edit_project_description_in_employee_mode(client, db_session, atlas_id):
    original = db_session.get(Project, atlas_id).description
    resp = await client.put(
        f"/projects/{atlas_id}/description", params={"view_mode": "employee"},
        json={"description": "employee mode write"}, headers=auth_headers("it"),
    )
    assert resp.status_code == 403

    db_session.expire_all()
    assert db_session.get(Project, atlas_id).description == original


@pytest.mark.parametrize("role", ["employee", "manager"])
async def test_unprivileged_role_asking_for_work_mode_is_still_denied(client, atlas_id, role):
    """view_mode=work in the query string is not a privilege escalation —
    resolve_view_mode pins these roles to employee mode before the write
    gate ever sees it."""
    resp = await client.put(
        f"/projects/{atlas_id}/description", params={"view_mode": "work"},
        json={"description": "nope"}, headers=auth_headers(role),
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Not-found beats nothing-happened, and 404 is not used to hide a 403.
# ---------------------------------------------------------------------------

async def test_hr_patch_on_missing_person_is_404(client):
    resp = await client.patch(
        "/employees/does-not-exist", params={"view_mode": "work"},
        json={"job_title": "Ghost"}, headers=auth_headers("hr"),
    )
    assert resp.status_code == 404


async def test_authorization_is_checked_before_existence(client):
    """An unauthorized caller gets 403 for a person who doesn't exist,
    rather than 404 — otherwise the endpoint is an existence oracle for
    anyone who can send a PATCH."""
    resp = await client.patch(
        "/employees/does-not-exist", params={"view_mode": "work"},
        json={"salary": "1.00"}, headers=auth_headers("employee"),
    )
    assert resp.status_code == 403
