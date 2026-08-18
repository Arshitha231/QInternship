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

from app.models import (
    AuditLog, CommunityLink, Employee, EmployeeActionRequest, Office, OrgUnit, Project,
)
from app.models.enums import AvailabilityStatus, CommunityLinkSource, EmploymentType
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
# Create employee — maker-checker, same as restrict/deactivate. POST
# /employees stages a request (202) and creates NOBODY; only the requester's
# own resolved approver, via approve_action_request, inserts the row.
#
# The extra wrinkle this action has and the other two don't: there is no
# target_employee_id until the approval lands, so the proposed person lives
# in the request's payload column and every "who is this about" surface has
# to read it from there (app.writes.request_subject_name).
# ---------------------------------------------------------------------------

def _org_unit_id(db_session) -> int:
    return db_session.query(OrgUnit).filter(OrgUnit.name == "Platform Engineering").one().id


def _office_id(db_session) -> int:
    return db_session.query(Office).filter(Office.name == "Test HQ").one().id


async def _request_create(client, db_session, requester_id="hr-actor-1", role="hr", **overrides):
    body = {
        "full_name": "Brand New Hire", "job_title": "Software Engineer",
        "org_unit_id": _org_unit_id(db_session), "work_email": "brand.new.hire@example.test",
        "employment_type": "fte",
    }
    body.update(overrides)
    return await client.post(
        "/employees", params={"view_mode": "work"}, json=body,
        headers=auth_headers(role, requester_id),
    )


def _employee_named(db_session, full_name: str) -> Employee | None:
    db_session.expire_all()
    return db_session.query(Employee).filter(Employee.full_name == full_name).first()


async def test_create_stages_a_pending_request_and_creates_nobody(client, db_session):
    requester, approver = _requester_with_approver(db_session, "create-stage")
    resp = await _request_create(
        client, db_session, requester_id=requester.id,
        full_name="Staged Not Created", work_email="staged.not.created@example.test",
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "pending"
    assert body["action_type"] == "create"
    assert body["approver_id"] == approver.id
    # No employee to point at yet — but the person is still named, from the
    # payload rather than from a FK.
    assert body["target_id"] is None
    assert body["target_name"] == "Staged Not Created"

    assert _employee_named(db_session, "Staged Not Created") is None


async def test_approving_a_create_actually_creates_the_employee(client, db_session):
    requester, approver = _requester_with_approver(db_session, "create-approve")
    resp = await _request_create(
        client, db_session, requester_id=requester.id,
        full_name="Approved Into Existence", work_email="approved.into.existence@example.test",
    )
    request_id = resp.json()["request_id"]

    approved = await _approve(client, request_id, approver_id=approver.id)
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"

    row = _employee_named(db_session, "Approved Into Existence")
    assert row is not None
    assert row.is_active is True
    assert row.availability_status is AvailabilityStatus.available
    assert row.hire_date == date.today()
    # The request now points at the person it made, so the audit trail can
    # get from "who approved this" to "who exists because of it".
    assert approved.json()["target_id"] == row.id


async def test_rejecting_a_create_creates_nobody(client, db_session):
    requester, approver = _requester_with_approver(db_session, "create-reject")
    resp = await _request_create(
        client, db_session, requester_id=requester.id,
        full_name="Rejected Never Existed", work_email="rejected.never.existed@example.test",
    )
    rejected = await _reject(
        client, resp.json()["request_id"], approver_id=approver.id, reason="headcount not approved",
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "rejected"
    assert _employee_named(db_session, "Rejected Never Existed") is None


async def test_create_approval_requires_being_the_resolved_approver(client, db_session):
    requester, _real_approver = _requester_with_approver(db_session, "create-wrongapprover")
    resp = await _request_create(
        client, db_session, requester_id=requester.id,
        full_name="Wrong Approver Hire", work_email="wrong.approver.hire@example.test",
    )
    denied = await _approve(client, resp.json()["request_id"], approver_id="someone-else-entirely")
    assert denied.status_code == 403
    assert _employee_named(db_session, "Wrong Approver Hire") is None


async def test_approved_create_carries_optional_fields_through_the_payload(client, db_session):
    requester, approver = _requester_with_approver(db_session, "create-optional")
    manager = _mkemp(db_session, "create-with-mgr", "Manager For New Hire")
    resp = await _request_create(
        client, db_session, requester_id=requester.id,
        full_name="Fully Specified Hire", preferred_name="Fully", job_title="Analyst",
        office_id=_office_id(db_session), manager_id=manager.id,
        work_email="fully.specified@example.test", work_phone="+1-555-0199",
        employment_type="contractor", hire_date="2026-03-01",
    )
    assert resp.status_code == 202, resp.text
    await _approve(client, resp.json()["request_id"], approver_id=approver.id)

    row = _employee_named(db_session, "Fully Specified Hire")
    assert row is not None
    assert row.preferred_name == "Fully"
    assert row.manager_id == manager.id
    # Round-tripped through JSON as a string and coerced back on the way in.
    assert row.hire_date == date(2026, 3, 1)
    assert row.employment_type is EmploymentType.contractor


async def test_create_duplicate_email_is_409_at_request_time(client, db_session):
    _requester_with_approver(db_session, "create-dup")
    _mkemp(db_session, "dup-email-existing", "Existing Person", work_email="dup@example.test")
    resp = await _request_create(
        client, db_session, requester_id="requester-create-dup",
        full_name="Duplicate Email Attempt", work_email="dup@example.test",
    )
    assert resp.status_code == 409


async def test_email_taken_while_pending_is_caught_at_approval(client, db_session):
    """The world moves while a request sits pending — so the same validation
    runs again on the way in, and a create that has become inapplicable
    fails at approval rather than colliding in the database."""
    requester, approver = _requester_with_approver(db_session, "create-raced")
    resp = await _request_create(
        client, db_session, requester_id=requester.id,
        full_name="Raced Hire", work_email="raced.hire@example.test",
    )
    request_id = resp.json()["request_id"]

    # Somebody else takes the address between staging and approval.
    _mkemp(db_session, "raced-winner", "Raced Winner", work_email="raced.hire@example.test")

    denied = await _approve(client, request_id, approver_id=approver.id)
    assert denied.status_code == 409, denied.text
    assert _employee_named(db_session, "Raced Hire") is None


async def test_create_missing_required_field_is_422(client, db_session):
    resp = await client.post(
        "/employees", params={"view_mode": "work"},
        json={"full_name": "Missing Fields"},
        headers=auth_headers("hr"),
    )
    assert resp.status_code == 422


async def test_create_invalid_org_unit_is_422(client, db_session):
    _requester_with_approver(db_session, "create-badunit")
    resp = await _request_create(
        client, db_session, requester_id="requester-create-badunit",
        full_name="Bad Org Unit", work_email="bad.org.unit@example.test", org_unit_id=999999,
    )
    assert resp.status_code == 422


async def test_create_invalid_manager_is_422(client, db_session):
    _requester_with_approver(db_session, "create-badmgr")
    resp = await _request_create(
        client, db_session, requester_id="requester-create-badmgr",
        full_name="Bad Manager", work_email="bad.manager@example.test", manager_id="does-not-exist",
    )
    assert resp.status_code == 422


async def test_create_with_no_reachable_approver_is_422(client, db_session):
    """A requester with nobody above them cannot self-serve a create — the
    request is refused outright rather than applied unapproved."""
    loner = _mkemp(db_session, "create-no-approver", "Create No Approver", manager_id=None)
    resp = await _request_create(
        client, db_session, requester_id=loner.id,
        full_name="Unapprovable Hire", work_email="unapprovable.hire@example.test",
    )
    assert resp.status_code == 422
    assert _employee_named(db_session, "Unapprovable Hire") is None


@pytest.mark.parametrize("role", ["employee", "manager", "it"])
async def test_non_hr_cannot_request_create(client, db_session, role):
    resp = await _request_create(
        client, db_session, role=role, requester_id=f"nonhr-create-{role}",
        full_name=f"Unauthorized Create {role}", work_email=f"unauthorized.{role}@example.test",
    )
    assert resp.status_code == 403


async def test_created_employee_is_findable_only_after_approval(client, db_session):
    requester, approver = _requester_with_approver(db_session, "create-findable")
    resp = await _request_create(
        client, db_session, requester_id=requester.id,
        full_name="Findable New Hire", work_email="findable.new.hire@example.test",
    )
    request_id = resp.json()["request_id"]

    before = await client.get(
        "/people", params={"name": "Findable New Hire", "view_mode": "work"}, headers=auth_headers("hr"))
    assert before.json() == []

    await _approve(client, request_id, approver_id=approver.id)
    after = await client.get(
        "/people", params={"name": "Findable New Hire", "view_mode": "work"}, headers=auth_headers("hr"))
    assert [p["full_name"] for p in after.json()] == ["Findable New Hire"]


async def test_create_request_writes_an_audit_row(client, db_session):
    _requester_with_approver(db_session, "create-audit")
    resp = await _request_create(
        client, db_session, requester_id="requester-create-audit",
        full_name="Audited New Hire", work_email="audited.new.hire@example.test",
    )
    assert resp.status_code == 202, resp.text
    row = (
        db_session.query(AuditLog).filter(AuditLog.action == "request_creation")
        .order_by(AuditLog.id.desc()).first()
    )
    assert row is not None
    assert row.actor_id == "requester-create-audit"


async def test_pending_create_appears_in_the_approvers_queue_by_name(client, db_session):
    """The approver's queue has to name a person who has no row yet — the
    one surface where target_name cannot come from a join."""
    requester, approver = _requester_with_approver(db_session, "create-queue")
    await _request_create(
        client, db_session, requester_id=requester.id,
        full_name="Queued Hire", work_email="queued.hire@example.test",
    )
    queue = await client.get(
        "/employee_action_requests", headers=auth_headers("hr", approver.id))
    assert queue.status_code == 200
    mine = [r for r in queue.json()["requests"] if r["action_type"] == "create"]
    assert any(
        r["target_name"] == "Queued Hire" and r["requested_by_name"] == requester.full_name
        for r in mine
    )


# ---------------------------------------------------------------------------
# The mentor answer on the create form. Not an employees column — it becomes
# an official community_links row owned by the new hire, byte-identical to
# what auto_assign_mentors would have created, so the sweep recognizes it.
# ---------------------------------------------------------------------------

async def test_approved_create_with_a_mentor_adds_the_community_link(client, db_session):
    requester, approver = _requester_with_approver(db_session, "create-mentor")
    mentor = _mkemp(db_session, "chosen-mentor", "Chosen Mentor")
    resp = await _request_create(
        client, db_session, requester_id=requester.id,
        full_name="Mentored Hire", work_email="mentored.hire@example.test", mentor_id=mentor.id,
    )
    assert resp.status_code == 202, resp.text
    # Staged only — no link yet, because there is no owner for it to belong to.
    assert db_session.query(CommunityLink).filter(
        CommunityLink.contact_employee_id == mentor.id).count() == 0

    await _approve(client, resp.json()["request_id"], approver_id=approver.id)

    new_hire = _employee_named(db_session, "Mentored Hire")
    link = db_session.query(CommunityLink).filter(
        CommunityLink.owner_employee_id == new_hire.id).one()
    assert link.contact_employee_id == mentor.id
    assert link.role_label == "mentor"
    assert link.is_mentor_link is True
    # Official, not personal: HR chose it, so the new hire can't edit or
    # delete it — same as any swept-in mentor link.
    assert link.source is CommunityLinkSource.official


async def test_mentor_link_shows_up_in_the_new_hires_own_community_graph(client, db_session):
    requester, approver = _requester_with_approver(db_session, "create-mentor-graph")
    mentor = _mkemp(db_session, "graph-mentor", "Graph Mentor")
    resp = await _request_create(
        client, db_session, requester_id=requester.id,
        full_name="Graph Mentored Hire", work_email="graph.mentored@example.test",
        mentor_id=mentor.id,
    )
    await _approve(client, resp.json()["request_id"], approver_id=approver.id)
    new_hire = _employee_named(db_session, "Graph Mentored Hire")

    graph = await client.get("/community_links", headers=auth_headers("employee", new_hire.id))
    assert graph.status_code == 200
    mentor_rows = [r for r in graph.json() if r["is_mentor_link"]]
    assert [r["contact_employee_id"] for r in mentor_rows] == [mentor.id]


async def test_create_with_an_inactive_mentor_is_422(client, db_session):
    _requester_with_approver(db_session, "create-deadmentor")
    dead_mentor = _mkemp(db_session, "inactive-mentor", "Inactive Mentor", is_active=False)
    resp = await _request_create(
        client, db_session, requester_id="requester-create-deadmentor",
        full_name="Bad Mentor Hire", work_email="bad.mentor.hire@example.test",
        mentor_id=dead_mentor.id,
    )
    assert resp.status_code == 422


async def test_mentor_deactivated_while_pending_is_caught_at_approval(client, db_session):
    requester, approver = _requester_with_approver(db_session, "create-mentor-left")
    mentor = _mkemp(db_session, "departing-mentor", "Departing Mentor")
    resp = await _request_create(
        client, db_session, requester_id=requester.id,
        full_name="Orphaned Mentee", work_email="orphaned.mentee@example.test", mentor_id=mentor.id,
    )
    request_id = resp.json()["request_id"]

    mentor.is_active = False
    db_session.commit()

    denied = await _approve(client, request_id, approver_id=approver.id)
    assert denied.status_code == 409, denied.text
    # Refused as a whole: no half-created employee with a missing mentor.
    assert _employee_named(db_session, "Orphaned Mentee") is None


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


# ---------------------------------------------------------------------------
# GET /employees/deactivated — the one read path that surfaces
# is_active=False records at all. Everything else in the app treats them as
# nonexistent, which is what left reactivate unreachable without an id.
# ---------------------------------------------------------------------------

async def _deactivated(client, role="hr", user_id="hr-lister-1"):
    resp = await client.get(
        "/employees/deactivated", params={"view_mode": "work"}, headers=auth_headers(role, user_id))
    assert resp.status_code == 200, resp.text
    return resp.json()["employees"]


async def test_deactivated_list_shows_an_inactive_employee(client, db_session):
    requester, approver = _requester_with_approver(db_session, "dlist-shows")
    target = _mkemp(db_session, "dlist-shows-target", "Deactivated Lister Target")
    request_id = (await _request_deactivate(client, target.id, requester_id=requester.id)).json()["request_id"]
    await _approve(client, request_id, approver_id=approver.id)

    rows = await _deactivated(client)
    match = next((r for r in rows if r["id"] == target.id), None)
    assert match is not None
    assert match["full_name"] == "Deactivated Lister Target"
    assert match["deactivated_at"] is not None
    assert match["org_unit"] == "Platform Engineering"


async def test_deactivated_list_excludes_active_employees(client, db_session):
    active = _mkemp(db_session, "dlist-active", "Still Active Person")
    rows = await _deactivated(client)
    assert all(r["id"] != active.id for r in rows)


async def test_deactivated_list_includes_rows_with_no_deactivated_at(client, db_session):
    """Seeded-inactive (or pre-column) rows have deactivated_at NULL. They're
    still deactivated employees, and this is the only view that can see them
    — dropping them would make them permanently unreachable."""
    legacy = _mkemp(db_session, "dlist-legacy", "Legacy Inactive Person", is_active=False)
    assert legacy.deactivated_at is None

    rows = await _deactivated(client)
    assert any(r["id"] == legacy.id for r in rows)


async def test_deactivated_list_sorts_newest_first_nulls_last(client, db_session):
    from datetime import datetime

    older = _mkemp(db_session, "dlist-sort-older", "Older Departure", is_active=False)
    newer = _mkemp(db_session, "dlist-sort-newer", "Newer Departure", is_active=False)
    nulled = _mkemp(db_session, "dlist-sort-null", "Null Departure", is_active=False)
    older.deactivated_at = datetime(2024, 1, 1, 12, 0, 0)
    newer.deactivated_at = datetime(2026, 1, 1, 12, 0, 0)
    nulled.deactivated_at = None
    db_session.commit()

    ids = [r["id"] for r in await _deactivated(client)]
    assert ids.index(newer.id) < ids.index(older.id)
    assert ids.index(older.id) < ids.index(nulled.id)


async def test_deactivated_list_omits_salary_and_dob(client, db_session):
    """Narrow by construction — identity and placement, nothing else. Not
    because HR/work couldn't read those elsewhere, but because this
    carve-out into is_active=False territory stays as small as it can."""
    from decimal import Decimal

    target = _mkemp(db_session, "dlist-narrow", "Narrow Fields Target", is_active=False,
                    salary=Decimal("123456.00"), salary_currency="USD", date_of_birth=date(1990, 1, 1))
    rows = await _deactivated(client)
    match = next(r for r in rows if r["id"] == target.id)
    assert set(match) == {"id", "full_name", "job_title", "org_unit", "work_email", "deactivated_at"}


@pytest.mark.parametrize("role", ["employee", "manager", "it"])
async def test_deactivated_list_is_hr_only(client, role):
    resp = await client.get(
        "/employees/deactivated", params={"view_mode": "work"}, headers=auth_headers(role))
    assert resp.status_code == 403


async def test_deactivated_list_denied_in_employee_mode(client):
    resp = await client.get(
        "/employees/deactivated", params={"view_mode": "employee"}, headers=auth_headers("hr"))
    assert resp.status_code == 403


async def test_deactivated_path_does_not_shadow_the_person_id_routes(client, db_session):
    """GET /employees/deactivated is a static path sitting beside
    /employees/{person_id}/... ones. Asserts the literal segment didn't get
    captured as a person_id by some other route, and that the id-shaped
    routes still work — the classic failure mode for this shape."""
    requester, approver = _requester_with_approver(db_session, "dlist-noshadow")
    target = _mkemp(db_session, "dlist-noshadow-target", "No Shadow Target")

    # An id-shaped POST still routes to the deactivate handler, not here.
    staged = await _request_deactivate(client, target.id, requester_id=requester.id)
    assert staged.status_code == 200, staged.text
    await _approve(client, staged.json()["request_id"], approver_id=approver.id)

    # And the literal word isn't treated as an employee id by the PATCH route.
    patched = await client.patch(
        "/employees/deactivated", params={"view_mode": "work"},
        json={"job_title": "Should Not Exist"}, headers=auth_headers("hr"),
    )
    assert patched.status_code == 404


async def test_reactivating_from_the_list_removes_it_from_the_list(client, db_session):
    """The whole point of the view: it makes reactivate reachable."""
    requester, approver = _requester_with_approver(db_session, "dlist-roundtrip")
    target = _mkemp(db_session, "dlist-roundtrip-target", "Round Trip Target")
    request_id = (await _request_deactivate(client, target.id, requester_id=requester.id)).json()["request_id"]
    await _approve(client, request_id, approver_id=approver.id)

    assert any(r["id"] == target.id for r in await _deactivated(client))

    resp = await client.post(
        f"/employees/{target.id}/reactivate", params={"view_mode": "work"}, headers=auth_headers("hr"))
    assert resp.status_code == 200, resp.text

    assert all(r["id"] != target.id for r in await _deactivated(client))
    # Reachable through the ordinary read path again.
    visible = await client.get(
        f"/people/{target.id}", params={"view_mode": "work"}, headers=auth_headers("employee"))
    assert visible.status_code == 200


async def test_deactivated_list_writes_an_audit_row(client, db_session):
    _mkemp(db_session, "dlist-audit", "Audit List Target", is_active=False)
    await _deactivated(client, user_id="hr-dlist-auditor")

    row = (
        db_session.query(AuditLog).filter(AuditLog.action == "list_deactivated_employees")
        .order_by(AuditLog.id.desc()).first()
    )
    assert row is not None
    assert row.actor_id == "hr-dlist-auditor"
    assert row.result_count >= 1
