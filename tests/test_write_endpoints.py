"""Write endpoints: HR internal-field edits and IT project descriptions.

The point of these tests is that authorization is enforced on the WRITE
itself. Every denial case below calls the endpoint directly with whatever
role and view_mode it likes — there is no preceding read to have been
filtered, and no frontend to have hidden a button. If the only thing
stopping an employee from setting their own salary were a hidden form, all
of these would pass anyway.
"""
from datetime import date

import pytest

from app.models import AuditLog, Employee, EmployeeActionRequest, Office, OrgUnit, Project
from app.models.enums import AvailabilityStatus, EmploymentType
from tests.conftest import auth_headers

ALL_ROLES = ["employee", "manager", "hr", "it"]


@pytest.fixture
def atlas_id(db_session):
    return db_session.query(Project).filter(Project.name == "Project Atlas").one().id


def _mkemp(db_session, id_, full_name, **overrides) -> Employee:
    """A throwaway employee dedicated to one test, never one of conftest's
    shared fixture people (mgr-1/report-1/stranger-1/...) — those are relied
    on elsewhere in the suite to stay in their seeded state, and this test
    database is session-scoped (see conftest.py), so mutating a shared
    fixture here would leak into every test that runs after this one."""
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


# ---------------------------------------------------------------------------
# availability_status: "restricted" is now maker-checker only (see the
# request/approve section below) — a generic PATCH may still set
# available/away, but attempting "restricted" through it is refused,
# telling the caller to use POST /employees/{id}/restrict instead. The
# enforcement side (is_record_visible) already exists and is tested
# exhaustively in tests/test_visibility.py.
# ---------------------------------------------------------------------------

async def test_hr_can_unrestrict_a_profile(client, db_session):
    """Unrestricting stays a single-actor, immediate action — the maker-
    checker requirement is specifically about the transition INTO
    restricted, not out of it."""
    _mkemp(db_session, "restrict-target-2", "Restrict Target Two",
           availability_status=AvailabilityStatus.restricted)

    still_hidden = await client.get(
        "/people/restrict-target-2", params={"view_mode": "work"}, headers=auth_headers("employee"))
    assert still_hidden.status_code == 404

    resp = await client.patch(
        "/employees/restrict-target-2", params={"view_mode": "work"},
        json={"availability_status": "available"}, headers=auth_headers("hr"),
    )
    assert resp.status_code == 200, resp.text

    now_visible = await client.get(
        "/people/restrict-target-2", params={"view_mode": "work"}, headers=auth_headers("employee"))
    assert now_visible.status_code == 200


async def test_patch_with_restricted_status_is_refused(client, db_session):
    emp = _mkemp(db_session, "restrict-via-patch-1", "Restrict Via Patch Attempt")
    resp = await client.patch(
        f"/employees/{emp.id}", params={"view_mode": "work"},
        json={"availability_status": "restricted"}, headers=auth_headers("hr"),
    )
    assert resp.status_code == 422
    db_session.expire_all()
    assert db_session.get(Employee, emp.id).availability_status is AvailabilityStatus.available


async def test_invalid_availability_status_value_is_422(client, db_session):
    _mkemp(db_session, "restrict-target-5", "Restrict Target Five")
    resp = await client.patch(
        "/employees/restrict-target-5", params={"view_mode": "work"},
        json={"availability_status": "banned"}, headers=auth_headers("hr"),
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# manager_id: needed before HR can deactivate a manager who still has
# active direct reports (the block-until-reassigned rule below).
# ---------------------------------------------------------------------------

async def test_hr_can_reassign_manager(client, db_session):
    old_mgr = _mkemp(db_session, "reassign-old-mgr", "Old Manager")
    new_mgr = _mkemp(db_session, "reassign-new-mgr", "New Manager")
    report = _mkemp(db_session, "reassign-report", "A Report", manager_id=old_mgr.id)

    resp = await client.patch(
        f"/employees/{report.id}", params={"view_mode": "work"},
        json={"manager_id": new_mgr.id}, headers=auth_headers("hr"),
    )
    assert resp.status_code == 200, resp.text

    db_session.expire_all()
    assert db_session.get(Employee, report.id).manager_id == new_mgr.id


async def test_cannot_set_self_as_own_manager(client, db_session):
    emp = _mkemp(db_session, "self-mgr-1", "Self Manager Attempt")
    resp = await client.patch(
        f"/employees/{emp.id}", params={"view_mode": "work"},
        json={"manager_id": emp.id}, headers=auth_headers("hr"),
    )
    assert resp.status_code == 422


@pytest.mark.parametrize("bad_manager_id", ["does-not-exist", "will-be-inactive"])
async def test_manager_id_must_reference_an_active_employee(client, db_session, bad_manager_id):
    if bad_manager_id == "will-be-inactive":
        _mkemp(db_session, bad_manager_id, "Inactive Manager Candidate", is_active=False)
    emp = _mkemp(db_session, f"mgr-check-target-{bad_manager_id}", "Manager Check Target")
    resp = await client.patch(
        f"/employees/{emp.id}", params={"view_mode": "work"},
        json={"manager_id": bad_manager_id}, headers=auth_headers("hr"),
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Deactivate and restrict are both maker-checker now: the POST stages a
# request (blocking, 409, on the same up-front checks as before — active
# direct reports, self-action, already-inactive/restricted) but does NOT
# apply anything. Only approve_action_request, called by the REQUESTER's
# own resolved approver, actually flips is_active or availability_status.
# ---------------------------------------------------------------------------

async def _request_deactivate(client, target_id, requester_role="hr", requester_id="hr-actor-1"):
    return await client.post(
        f"/employees/{target_id}/deactivate", params={"view_mode": "work"},
        headers=auth_headers(requester_role, requester_id),
    )


async def _request_restrict(client, target_id, requester_role="hr", requester_id="hr-actor-1"):
    return await client.post(
        f"/employees/{target_id}/restrict", params={"view_mode": "work"},
        headers=auth_headers(requester_role, requester_id),
    )


async def _approve(client, request_id, approver_role="hr", approver_id="approver-1"):
    return await client.post(
        f"/employee_action_requests/{request_id}/approve", params={"view_mode": "work"},
        headers=auth_headers(approver_role, approver_id),
    )


async def _reject(client, request_id, approver_role="hr", approver_id="approver-1", reason=None):
    return await client.post(
        f"/employee_action_requests/{request_id}/reject", params={"view_mode": "work"},
        json={"reason": reason}, headers=auth_headers(approver_role, approver_id),
    )


def _requester_with_approver(db_session, suffix: str) -> tuple[Employee, Employee]:
    """A requester whose manager (the approver _resolve_approver should
    find) is active and available — the simple, common case every
    request/approve test below builds on."""
    approver = _mkemp(db_session, f"approver-{suffix}", f"Approver {suffix}")
    requester = _mkemp(db_session, f"requester-{suffix}", f"Requester {suffix}", manager_id=approver.id)
    return requester, approver


async def test_deactivate_stages_a_pending_request_not_immediate(client, db_session):
    requester, approver = _requester_with_approver(db_session, "deact-stage")
    target = _mkemp(db_session, "deact-stage-target", "Deactivate Stage Target")

    resp = await _request_deactivate(client, target.id, requester_id=requester.id)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "pending"
    assert body["action_type"] == "deactivate"
    assert body["approver_id"] == approver.id

    db_session.expire_all()
    assert db_session.get(Employee, target.id).is_active is True  # not applied yet


async def test_restrict_stages_a_pending_request_not_immediate(client, db_session):
    requester, approver = _requester_with_approver(db_session, "restrict-stage")
    target = _mkemp(db_session, "restrict-stage-target", "Restrict Stage Target")

    resp = await _request_restrict(client, target.id, requester_id=requester.id)
    assert resp.status_code == 200, resp.text
    assert resp.json()["approver_id"] == approver.id

    db_session.expire_all()
    assert db_session.get(Employee, target.id).availability_status is AvailabilityStatus.available

    # Not restricted yet — still visible to a non-HR caller.
    visible = await client.get(
        f"/people/{target.id}", params={"view_mode": "work"}, headers=auth_headers("employee"))
    assert visible.status_code == 200


async def test_deactivate_request_blocked_by_active_direct_reports(client, db_session):
    requester, _approver = _requester_with_approver(db_session, "deact-blocked")
    manager = _mkemp(db_session, "deact-blocked-mgr", "Blocked Manager")
    report = _mkemp(db_session, "deact-blocked-report", "Blocked Report", manager_id=manager.id)

    resp = await _request_deactivate(client, manager.id, requester_id=requester.id)
    assert resp.status_code == 409, resp.text
    reports = resp.json()["detail"]["active_direct_reports"]
    assert {r["id"] for r in reports} == {report.id}


async def test_deactivate_request_succeeds_after_reassigning_reports(client, db_session):
    requester, _approver = _requester_with_approver(db_session, "deact-reassign")
    old_manager = _mkemp(db_session, "deact-reassign-old-mgr", "Old Manager To Deactivate")
    new_manager = _mkemp(db_session, "deact-reassign-new-mgr", "New Manager")
    report = _mkemp(db_session, "deact-reassign-report", "Reassignable Report", manager_id=old_manager.id)

    await client.patch(
        f"/employees/{report.id}", params={"view_mode": "work"},
        json={"manager_id": new_manager.id}, headers=auth_headers("hr"),
    )
    resp = await _request_deactivate(client, old_manager.id, requester_id=requester.id)
    assert resp.status_code == 200, resp.text


async def test_approving_deactivation_applies_it_and_clears_delegates(client, db_session):
    requester, approver = _requester_with_approver(db_session, "deact-approve")
    away_person = _mkemp(db_session, "deact-approve-delegator", "Away Person")
    target = _mkemp(db_session, "deact-approve-target", "Deactivate Approve Target")
    away_person.delegate_id = target.id
    db_session.commit()

    request_resp = await _request_deactivate(client, target.id, requester_id=requester.id)
    assert request_resp.status_code == 200, request_resp.text
    request_id = request_resp.json()["request_id"]

    db_session.expire_all()
    assert db_session.get(Employee, target.id).is_active is True  # still not applied

    approve_resp = await _approve(client, request_id, approver_id=approver.id)
    assert approve_resp.status_code == 200, approve_resp.text
    assert approve_resp.json()["status"] == "approved"

    db_session.expire_all()
    row = db_session.get(Employee, target.id)
    assert row.is_active is False
    assert row.deactivated_at is not None
    assert db_session.get(Employee, "deact-approve-delegator").delegate_id is None


async def test_approving_restriction_applies_it(client, db_session):
    requester, approver = _requester_with_approver(db_session, "restrict-approve")
    target = _mkemp(db_session, "restrict-approve-target", "Restrict Approve Target")

    request_resp = await _request_restrict(client, target.id, requester_id=requester.id)
    request_id = request_resp.json()["request_id"]

    approve_resp = await _approve(client, request_id, approver_id=approver.id)
    assert approve_resp.status_code == 200, approve_resp.text

    db_session.expire_all()
    assert db_session.get(Employee, target.id).availability_status is AvailabilityStatus.restricted
    hidden = await client.get(
        f"/people/{target.id}", params={"view_mode": "work"}, headers=auth_headers("employee"))
    assert hidden.status_code == 404


async def test_approve_requires_being_the_resolved_approver(client, db_session):
    requester, _real_approver = _requester_with_approver(db_session, "deact-wrongapprover")
    target = _mkemp(db_session, "deact-wrongapprover-target", "Wrong Approver Target")
    request_resp = await _request_deactivate(client, target.id, requester_id=requester.id)
    request_id = request_resp.json()["request_id"]

    # Some other HR identity, not the resolved approver.
    resp = await _approve(client, request_id, approver_id="someone-else-entirely")
    assert resp.status_code == 403

    db_session.expire_all()
    assert db_session.get(Employee, target.id).is_active is True


async def test_reject_action_request_does_not_apply_the_change(client, db_session):
    requester, approver = _requester_with_approver(db_session, "deact-reject")
    target = _mkemp(db_session, "deact-reject-target", "Reject Target")
    request_resp = await _request_deactivate(client, target.id, requester_id=requester.id)
    request_id = request_resp.json()["request_id"]

    resp = await _reject(client, request_id, approver_id=approver.id, reason="not needed after all")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "rejected"

    db_session.expire_all()
    assert db_session.get(Employee, target.id).is_active is True
    row = db_session.get(EmployeeActionRequest, request_id)
    assert row.rejection_reason == "not needed after all"


async def test_approving_an_already_resolved_request_is_409(client, db_session):
    requester, approver = _requester_with_approver(db_session, "deact-doubleapprove")
    target = _mkemp(db_session, "deact-doubleapprove-target", "Double Approve Target")
    request_resp = await _request_deactivate(client, target.id, requester_id=requester.id)
    request_id = request_resp.json()["request_id"]

    first = await _approve(client, request_id, approver_id=approver.id)
    assert first.status_code == 200, first.text
    second = await _approve(client, request_id, approver_id=approver.id)
    assert second.status_code == 409


async def test_cannot_request_deactivation_of_own_record(client, db_session):
    _mkemp(db_session, "deactivate-self", "Self Deactivate Attempt", manager_id=None)
    resp = await _request_deactivate(client, "deactivate-self", requester_id="deactivate-self")
    assert resp.status_code == 403


async def test_deactivate_request_on_already_inactive_is_409(client, db_session):
    requester, _approver = _requester_with_approver(db_session, "deact-twice")
    target = _mkemp(db_session, "deactivate-twice", "Deactivate Twice", is_active=False)
    resp = await _request_deactivate(client, target.id, requester_id=requester.id)
    assert resp.status_code == 409


@pytest.mark.parametrize("role", ["employee", "manager", "it"])
async def test_non_hr_cannot_request_deactivation(client, db_session, role):
    requester = _mkemp(db_session, f"deact-nonhr-req-{role}", f"Non HR Requester {role}")
    target = _mkemp(db_session, f"deactivate-nonhr-{role}", "Non HR Deactivate Attempt")
    resp = await client.post(
        f"/employees/{target.id}/deactivate", params={"view_mode": "work"},
        headers=auth_headers(role, requester.id),
    )
    assert resp.status_code == 403
    db_session.expire_all()
    assert db_session.get(Employee, target.id).is_active is True


async def test_deactivated_employee_is_invisible_to_everyone_including_hr(client, db_session):
    """is_active=False is a different, stronger gate than availability_status
    == restricted — app.people.get_person returns None for every caller,
    HR included, once a record is inactive."""
    requester, approver = _requester_with_approver(db_session, "deact-invisible")
    target = _mkemp(db_session, "deact-invisible-target", "Deactivate Invisible Test")
    request_id = (await _request_deactivate(client, target.id, requester_id=requester.id)).json()["request_id"]
    await _approve(client, request_id, approver_id=approver.id)

    resp = await client.get(f"/people/{target.id}", params={"view_mode": "work"}, headers=auth_headers("hr"))
    assert resp.status_code == 404


async def test_deactivate_request_writes_an_audit_row(client, db_session):
    requester, approver = _requester_with_approver(db_session, "deact-audit")
    target = _mkemp(db_session, "deact-audit-target", "Deactivate Audit Test")
    request_id = (await _request_deactivate(client, target.id, requester_id=requester.id)).json()["request_id"]

    requested_row = (
        db_session.query(AuditLog).filter(AuditLog.action == "request_deactivation")
        .order_by(AuditLog.id.desc()).first()
    )
    assert requested_row is not None
    assert requested_row.actor_id == requester.id

    await _approve(client, request_id, approver_id=approver.id)
    approved_row = (
        db_session.query(AuditLog).filter(AuditLog.action == "approve_action_request")
        .order_by(AuditLog.id.desc()).first()
    )
    assert approved_row is not None
    assert approved_row.actor_id == approver.id


async def test_no_approver_available_is_422(client, db_session):
    requester = _mkemp(db_session, "no-approver-requester", "No Approver Requester", manager_id=None)
    target = _mkemp(db_session, "no-approver-target", "No Approver Target")
    resp = await _request_deactivate(client, target.id, requester_id=requester.id)
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Approver resolution: the requester's OWN chain, delegate first when away,
# then up one level, bounded and exhaustible. See app.writes._resolve_approver.
# ---------------------------------------------------------------------------

async def test_approver_escalates_past_away_manager_with_no_delegate(client, db_session):
    grandmanager = _mkemp(db_session, "escalate-1-grandmgr", "Grandmanager One")
    manager = _mkemp(db_session, "escalate-1-mgr", "Away Manager One",
                      manager_id=grandmanager.id, availability_status=AvailabilityStatus.away)
    requester = _mkemp(db_session, "escalate-1-req", "Escalate Requester One", manager_id=manager.id)
    target = _mkemp(db_session, "escalate-1-target", "Escalate Target One")

    resp = await _request_deactivate(client, target.id, requester_id=requester.id)
    assert resp.status_code == 200, resp.text
    assert resp.json()["approver_id"] == grandmanager.id


async def test_approver_uses_delegate_when_manager_is_away(client, db_session):
    delegate = _mkemp(db_session, "escalate-2-delegate", "Covering Delegate Two")
    manager = _mkemp(db_session, "escalate-2-mgr", "Away Manager Two",
                      availability_status=AvailabilityStatus.away, delegate_id=delegate.id)
    requester = _mkemp(db_session, "escalate-2-req", "Escalate Requester Two", manager_id=manager.id)
    target = _mkemp(db_session, "escalate-2-target", "Escalate Target Two")

    resp = await _request_deactivate(client, target.id, requester_id=requester.id)
    assert resp.status_code == 200, resp.text
    assert resp.json()["approver_id"] == delegate.id


async def test_approver_delegate_who_is_also_away_is_skipped(client, db_session):
    grandmanager = _mkemp(db_session, "escalate-3-grandmgr", "Grandmanager Three")
    also_away_delegate = _mkemp(db_session, "escalate-3-delegate", "Also Away Delegate Three",
                                 availability_status=AvailabilityStatus.away)
    manager = _mkemp(db_session, "escalate-3-mgr", "Away Manager Three", manager_id=grandmanager.id,
                      availability_status=AvailabilityStatus.away, delegate_id=also_away_delegate.id)
    requester = _mkemp(db_session, "escalate-3-req", "Escalate Requester Three", manager_id=manager.id)
    target = _mkemp(db_session, "escalate-3-target", "Escalate Target Three")

    resp = await _request_deactivate(client, target.id, requester_id=requester.id)
    assert resp.status_code == 200, resp.text
    assert resp.json()["approver_id"] == grandmanager.id


async def test_approver_skips_inactive_manager(client, db_session):
    grandmanager = _mkemp(db_session, "escalate-4-grandmgr", "Grandmanager Four")
    manager = _mkemp(db_session, "escalate-4-mgr", "Inactive Manager Four",
                      manager_id=grandmanager.id, is_active=False)
    requester = _mkemp(db_session, "escalate-4-req", "Escalate Requester Four", manager_id=manager.id)
    target = _mkemp(db_session, "escalate-4-target", "Escalate Target Four")

    resp = await _request_deactivate(client, target.id, requester_id=requester.id)
    assert resp.status_code == 200, resp.text
    assert resp.json()["approver_id"] == grandmanager.id


async def test_no_approver_when_whole_chain_is_unavailable(client, db_session):
    top = _mkemp(db_session, "escalate-5-top", "Top Away No Delegate", availability_status=AvailabilityStatus.away)
    manager = _mkemp(db_session, "escalate-5-mgr", "Middle Away No Delegate",
                      manager_id=top.id, availability_status=AvailabilityStatus.away)
    requester = _mkemp(db_session, "escalate-5-req", "Escalate Requester Five", manager_id=manager.id)
    target = _mkemp(db_session, "escalate-5-target", "Escalate Target Five")

    resp = await _request_deactivate(client, target.id, requester_id=requester.id)
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Pending approvals list — identity-scoped, not role-scoped.
# ---------------------------------------------------------------------------

async def test_list_pending_approvals_is_scoped_to_this_identity(client, db_session):
    requester_a, approver_a = _requester_with_approver(db_session, "list-a")
    requester_b, approver_b = _requester_with_approver(db_session, "list-b")
    target_a = _mkemp(db_session, "list-target-a", "List Target A")
    target_b = _mkemp(db_session, "list-target-b", "List Target B")
    await _request_deactivate(client, target_a.id, requester_id=requester_a.id)
    await _request_deactivate(client, target_b.id, requester_id=requester_b.id)

    resp = await client.get(
        "/employee_action_requests", params={"view_mode": "work"}, headers=auth_headers("hr", approver_a.id))
    assert resp.status_code == 200, resp.text
    targets = {r["target_id"] for r in resp.json()["requests"]}
    assert target_a.id in targets
    assert target_b.id not in targets


# ---------------------------------------------------------------------------
# Notifications — the maker-checker flow's "the row is the delivery"
# reuse of app/notifications.py's existing shape.
# ---------------------------------------------------------------------------

async def test_notifications_fire_on_request_and_on_resolution(client, db_session):
    from app.models import Notification
    from app.models.enums import NotificationKind

    requester, approver = _requester_with_approver(db_session, "notify")
    target = _mkemp(db_session, "notify-target", "Notify Target")

    request_id = (await _request_deactivate(client, target.id, requester_id=requester.id)).json()["request_id"]
    requested_notification = (
        db_session.query(Notification)
        .filter(Notification.kind == NotificationKind.action_approval_requested,
                Notification.recipient_id == approver.id)
        .order_by(Notification.id.desc()).first()
    )
    assert requested_notification is not None
    assert requested_notification.subject_employee_id == target.id

    await _approve(client, request_id, approver_id=approver.id)
    approved_notification = (
        db_session.query(Notification)
        .filter(Notification.kind == NotificationKind.action_approved,
                Notification.recipient_id == requester.id)
        .order_by(Notification.id.desc()).first()
    )
    assert approved_notification is not None


# ---------------------------------------------------------------------------
# Reactivate.
# ---------------------------------------------------------------------------

async def test_hr_can_reactivate(client, db_session):
    requester, approver = _requester_with_approver(db_session, "reactivate-setup")
    emp = _mkemp(db_session, "reactivate-1", "Reactivate Test")
    request_id = (await _request_deactivate(client, emp.id, requester_id=requester.id)).json()["request_id"]
    await _approve(client, request_id, approver_id=approver.id)

    resp = await client.post(
        f"/employees/{emp.id}/reactivate", params={"view_mode": "work"}, headers=auth_headers("hr"),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_active"] is True

    db_session.expire_all()
    row = db_session.get(Employee, emp.id)
    assert row.is_active is True
    assert row.deactivated_at is None

    # Visible again through the ordinary read path.
    visible = await client.get(f"/people/{emp.id}", params={"view_mode": "work"}, headers=auth_headers("employee"))
    assert visible.status_code == 200


async def test_reactivate_already_active_is_409(client, db_session):
    emp = _mkemp(db_session, "reactivate-already-active", "Already Active")
    resp = await client.post(
        f"/employees/{emp.id}/reactivate", params={"view_mode": "work"}, headers=auth_headers("hr"),
    )
    assert resp.status_code == 409


@pytest.mark.parametrize("role", ["employee", "manager", "it"])
async def test_non_hr_cannot_reactivate(client, db_session, role):
    emp = _mkemp(db_session, f"reactivate-nonhr-{role}", "Non HR Reactivate Attempt", is_active=False)
    resp = await client.post(
        f"/employees/{emp.id}/reactivate", params={"view_mode": "work"}, headers=auth_headers(role),
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Create employee.
# ---------------------------------------------------------------------------

def _org_unit_id(db_session) -> int:
    return db_session.query(OrgUnit).filter(OrgUnit.name == "Platform Engineering").one().id


def _office_id(db_session) -> int:
    return db_session.query(Office).filter(Office.name == "Test HQ").one().id


async def test_hr_can_create_employee_with_required_fields_only(client, db_session):
    resp = await client.post(
        "/employees", params={"view_mode": "work"},
        json={
            "full_name": "Brand New Hire", "job_title": "Software Engineer",
            "org_unit_id": _org_unit_id(db_session), "work_email": "brand.new.hire@example.test",
            "employment_type": "fte",
        },
        headers=auth_headers("hr", "hr-creator-1"),
    )
    assert resp.status_code == 201, resp.text
    new_id = resp.json()["id"]

    db_session.expire_all()
    row = db_session.get(Employee, new_id)
    assert row.full_name == "Brand New Hire"
    assert row.is_active is True
    assert row.availability_status is AvailabilityStatus.available
    assert row.hire_date == date.today()


async def test_hr_can_create_employee_with_optional_fields(client, db_session):
    manager = _mkemp(db_session, "create-with-mgr", "Manager For New Hire")
    resp = await client.post(
        "/employees", params={"view_mode": "work"},
        json={
            "full_name": "Fully Specified Hire", "preferred_name": "Fully", "job_title": "Analyst",
            "org_unit_id": _org_unit_id(db_session), "office_id": _office_id(db_session),
            "manager_id": manager.id, "work_email": "fully.specified@example.test",
            "work_phone": "+1-555-0199", "employment_type": "contractor", "hire_date": "2026-03-01",
        },
        headers=auth_headers("hr"),
    )
    assert resp.status_code == 201, resp.text

    db_session.expire_all()
    row = db_session.get(Employee, resp.json()["id"])
    assert row.preferred_name == "Fully"
    assert row.manager_id == manager.id
    assert row.hire_date == date(2026, 3, 1)


async def test_create_duplicate_email_is_409(client, db_session):
    _mkemp(db_session, "dup-email-existing", "Existing Person", work_email="dup@example.test")
    resp = await client.post(
        "/employees", params={"view_mode": "work"},
        json={
            "full_name": "Duplicate Email Attempt", "job_title": "Engineer",
            "org_unit_id": _org_unit_id(db_session), "work_email": "dup@example.test",
            "employment_type": "fte",
        },
        headers=auth_headers("hr"),
    )
    assert resp.status_code == 409


async def test_create_missing_required_field_is_422(client, db_session):
    resp = await client.post(
        "/employees", params={"view_mode": "work"},
        json={"full_name": "Missing Fields"},
        headers=auth_headers("hr"),
    )
    assert resp.status_code == 422


async def test_create_invalid_org_unit_is_422(client):
    resp = await client.post(
        "/employees", params={"view_mode": "work"},
        json={
            "full_name": "Bad Org Unit", "job_title": "Engineer",
            "org_unit_id": 999999, "work_email": "bad.org.unit@example.test", "employment_type": "fte",
        },
        headers=auth_headers("hr"),
    )
    assert resp.status_code == 422


async def test_create_invalid_manager_is_422(client, db_session):
    resp = await client.post(
        "/employees", params={"view_mode": "work"},
        json={
            "full_name": "Bad Manager", "job_title": "Engineer",
            "org_unit_id": _org_unit_id(db_session), "work_email": "bad.manager@example.test",
            "employment_type": "fte", "manager_id": "does-not-exist",
        },
        headers=auth_headers("hr"),
    )
    assert resp.status_code == 422


@pytest.mark.parametrize("role", ["employee", "manager", "it"])
async def test_non_hr_cannot_create_employee(client, db_session, role):
    resp = await client.post(
        "/employees", params={"view_mode": "work"},
        json={
            "full_name": f"Unauthorized Create {role}", "job_title": "Engineer",
            "org_unit_id": _org_unit_id(db_session), "work_email": f"unauthorized.{role}@example.test",
            "employment_type": "fte",
        },
        headers=auth_headers(role),
    )
    assert resp.status_code == 403


async def test_created_employee_is_findable(client, db_session):
    resp = await client.post(
        "/employees", params={"view_mode": "work"},
        json={
            "full_name": "Findable New Hire", "job_title": "Engineer",
            "org_unit_id": _org_unit_id(db_session), "work_email": "findable.new.hire@example.test",
            "employment_type": "fte",
        },
        headers=auth_headers("hr"),
    )
    assert resp.status_code == 201, resp.text

    found = await client.get(
        "/people", params={"name": "Findable New Hire", "view_mode": "work"}, headers=auth_headers("hr"))
    assert found.status_code == 200
    assert any(p["id"] == resp.json()["id"] for p in found.json())


async def test_create_writes_an_audit_row(client, db_session):
    resp = await client.post(
        "/employees", params={"view_mode": "work"},
        json={
            "full_name": "Audited New Hire", "job_title": "Engineer",
            "org_unit_id": _org_unit_id(db_session), "work_email": "audited.new.hire@example.test",
            "employment_type": "fte",
        },
        headers=auth_headers("hr", "hr-create-auditor"),
    )
    assert resp.status_code == 201, resp.text
    row = (
        db_session.query(AuditLog).filter(AuditLog.action == "create_employee")
        .order_by(AuditLog.id.desc()).first()
    )
    assert row is not None
    assert row.actor_id == "hr-create-auditor"


# ---------------------------------------------------------------------------
# /org_units and /offices — the create-employee picker's lookups. Not
# sensitive (org_unit/office are already BASE_FIELDS on every profile), so
# any authenticated caller, not just HR.
# ---------------------------------------------------------------------------

async def test_list_org_units_any_authenticated_role(client, db_session):
    resp = await client.get("/org_units", headers=auth_headers("employee"))
    assert resp.status_code == 200, resp.text
    names = {u["name"] for u in resp.json()}
    assert "Platform Engineering" in names


async def test_list_offices_any_authenticated_role(client, db_session):
    resp = await client.get("/offices", headers=auth_headers("employee"))
    assert resp.status_code == 200, resp.text
    names = {o["name"] for o in resp.json()}
    assert "Test HQ" in names
