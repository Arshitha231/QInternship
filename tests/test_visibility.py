"""Step 5: replay real HTTP requests against the running FastAPI app and
assert on the actual response bodies — never inspect service internals.
"""
import pytest

from app.models import AuditLog
from app.people import MAX_RESULTS
from tests.conftest import auth_headers

# ---------------------------------------------------------------------------
# RBAC: hire_date / cost_centre are hr-only, regardless of relationship.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("role,expected_present", [
    ("employee", False),
    ("manager", False),
    ("hr", True),
])
async def test_hr_only_fields_by_role(client, role, expected_present):
    resp = await client.get("/people/stranger-1", headers=auth_headers(role))
    assert resp.status_code == 200
    body = resp.json()
    for field in ("hire_date", "cost_centre"):
        assert (field in body) is expected_present, (
            f"role={role}: expected '{field}' present={expected_present}, got body={body}"
        )


# ---------------------------------------------------------------------------
# ABAC: personal_mobile is own-profile-or-direct-manager, independent of role.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("role,caller_id,expected_present,case", [
    ("employee", "report-1", True, "own profile"),
    ("manager", "mgr-1", True, "actual direct manager, manager role"),
    ("employee", "mgr-1", True, "actual direct manager, employee role (relationship matters, not role)"),
    ("hr", "mgr-1", True, "actual direct manager, hr role"),
    ("employee", "stranger-1", False, "unrelated caller, employee role"),
    ("manager", "stranger-1", False, "manager ROLE alone is not the same as BEING the manager"),
    ("hr", "stranger-1", False, "hr role alone does not grant personal_mobile without the relationship"),
])
async def test_personal_mobile_abac(client, role, caller_id, expected_present, case):
    resp = await client.get("/people/report-1", headers=auth_headers(role, caller_id))
    assert resp.status_code == 200
    body = resp.json()
    assert ("personal_mobile" in body) is expected_present, f"{case}: body={body}"


async def test_absent_field_is_truly_missing_not_null(client):
    resp = await client.get("/people/stranger-1", headers=auth_headers("employee"))
    body = resp.json()
    assert "cost_centre" not in body
    # Confirm this isn't just json.dumps dropping None values generally —
    # a *legitimately* empty but visible field still comes through as null.
    assert "away_until_month" in body
    assert body["away_until_month"] is None


# ---------------------------------------------------------------------------
# Confidential project membership: members only, no manager bypass.
# ---------------------------------------------------------------------------

async def test_confidential_project_visible_to_member(client):
    resp = await client.get("/people/member-1", headers=auth_headers("employee", "member-1"))
    names = [p["project_name"] for p in resp.json()["project_history"]]
    assert "Project Secret" in names


async def test_confidential_project_hidden_from_non_member_manager(client):
    resp = await client.get("/people/member-1", headers=auth_headers("employee", "member-manager-1"))
    names = [p["project_name"] for p in resp.json()["project_history"]]
    assert "Project Secret" not in names


# ---------------------------------------------------------------------------
# Record-level restriction: empty result, never a 403.
# ---------------------------------------------------------------------------

async def test_restricted_record_get_person_is_404_not_403(client):
    resp = await client.get("/people/restricted-1", headers=auth_headers("employee"))
    assert resp.status_code == 404
    assert resp.status_code != 403


async def test_restricted_record_visible_to_hr(client):
    resp = await client.get("/people/restricted-1", headers=auth_headers("hr"))
    assert resp.status_code == 200
    assert resp.json()["full_name"] == "Rory Restricted"


async def test_restricted_record_missing_from_find_people_for_non_hr(client):
    resp = await client.get("/people", params={"name": "Rory"}, headers=auth_headers("employee"))
    assert resp.status_code == 200
    assert resp.json() == []  # empty result set, not an error


async def test_restricted_record_present_in_find_people_for_hr(client):
    resp = await client.get("/people", params={"name": "Rory"}, headers=auth_headers("hr"))
    assert resp.status_code == 200
    names = [p["full_name"] for p in resp.json()]
    assert "Rory Restricted" in names


async def test_no_match_and_no_access_look_identical(client):
    """The no-results message must be identical whether nobody matched or
    the caller lacks access."""
    no_match = await client.get("/people/does-not-exist", headers=auth_headers("employee"))
    no_access = await client.get("/people/restricted-1", headers=auth_headers("employee"))
    assert no_match.status_code == no_access.status_code == 404
    assert no_match.json() == no_access.json()


# ---------------------------------------------------------------------------
# Query-level: the result cap holds on a broad, unfiltered query.
# ---------------------------------------------------------------------------

async def test_result_cap_holds_on_broad_query(client):
    resp = await client.get("/people", headers=auth_headers("employee"))
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == MAX_RESULTS


# ---------------------------------------------------------------------------
# Every request writes an audit_log row — including ones that find nothing.
# ---------------------------------------------------------------------------

async def test_find_people_writes_audit_log(client, db_session):
    before = db_session.query(AuditLog).count()
    resp = await client.get("/people", params={"name": "Riley"}, headers=auth_headers("employee", "auditor-1"))
    assert resp.status_code == 200
    after = db_session.query(AuditLog).count()
    assert after == before + 1

    row = db_session.query(AuditLog).order_by(AuditLog.id.desc()).first()
    assert row.actor_id == "auditor-1"
    assert row.action == "find_people"
    assert row.result_count == 1


async def test_get_person_writes_audit_log_even_on_404(client, db_session):
    before = db_session.query(AuditLog).count()
    resp = await client.get("/people/does-not-exist", headers=auth_headers("employee", "auditor-2"))
    assert resp.status_code == 404
    after = db_session.query(AuditLog).count()
    assert after == before + 1  # audited even though nothing was found

    row = db_session.query(AuditLog).order_by(AuditLog.id.desc()).first()
    assert row.actor_id == "auditor-2"
    assert row.action == "get_person"
    assert row.result_count == 0


async def test_get_person_audit_reflects_restricted_denial(client, db_session):
    """The audit trail is allowed to know more than the caller's response
    does: a restricted-record denial and a true 404 look the same over the
    wire, but the audit log still records that a record existed."""
    resp = await client.get("/people/restricted-1", headers=auth_headers("employee", "auditor-3"))
    assert resp.status_code == 404

    row = db_session.query(AuditLog).order_by(AuditLog.id.desc()).first()
    assert row.actor_id == "auditor-3"
    assert row.result_count == 0
    assert row.fields_returned == "[]"
