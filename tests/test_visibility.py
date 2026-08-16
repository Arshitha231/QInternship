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


# ---------------------------------------------------------------------------
# ARCHITECTURE_2.md §15 item 6 / Phase 3 Round 2: a restricted employee
# referenced as someone else's manager or delegate must be as invisible in
# that role as they are as a direct search result -- fixed by routing
# manager/delegate attachment through enforce()+compile_query()
# (app.query_compiler.enforced_person_ref) instead of a raw db.get() with
# no visibility check. Fixture: "managed-by-restricted-1" (manager is
# restricted-1), "delegates-to-restricted-1" (delegate is restricted-1,
# manager is mgr-1) -- see tests/conftest.py.
# ---------------------------------------------------------------------------

async def test_get_person_manager_hidden_when_manager_is_restricted_non_hr(client):
    resp = await client.get("/people/managed-by-restricted-1", headers=auth_headers("employee"))
    assert resp.status_code == 200
    assert "manager" not in resp.json()


async def test_get_person_manager_visible_to_hr_even_when_restricted(client):
    resp = await client.get("/people/managed-by-restricted-1", headers=auth_headers("hr"))
    assert resp.status_code == 200
    assert resp.json()["manager"]["id"] == "restricted-1"


async def test_find_people_manager_hidden_when_manager_is_restricted_non_hr(client):
    resp = await client.get("/people", params={"name": "Quinn Reports"}, headers=auth_headers("employee"))
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert "manager" not in body[0]


async def test_find_people_manager_visible_to_hr_even_when_restricted(client):
    resp = await client.get("/people", params={"name": "Quinn Reports"}, headers=auth_headers("hr"))
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["manager"]["id"] == "restricted-1"


async def test_find_people_delegate_hidden_when_delegate_is_restricted_non_hr(client):
    resp = await client.get("/people", params={"name": "Drew Delegator"}, headers=auth_headers("employee"))
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert "delegate" not in body[0]


async def test_find_people_delegate_visible_to_hr_even_when_restricted(client):
    resp = await client.get("/people", params={"name": "Drew Delegator"}, headers=auth_headers("hr"))
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["delegate"]["id"] == "restricted-1"


async def test_org_chain_delegate_hidden_when_delegate_is_restricted_non_hr(client):
    # role=manager (not employee) so the "down" direction's own RBAC gate
    # doesn't short-circuit the response before delegate visibility is
    # even reached -- this test is specifically about the delegate
    # reference, not about who can walk the chain at all.
    resp = await client.get(
        "/people/mgr-1/org-chart", params={"direction": "down", "depth": 1},
        headers=auth_headers("manager", "mgr-1"),
    )
    assert resp.status_code == 200
    nodes = resp.json()
    drew = next(n for n in nodes if n["id"] == "delegates-to-restricted-1")
    assert drew["delegate"] is None


async def test_org_chain_delegate_visible_to_hr_even_when_restricted(client):
    resp = await client.get(
        "/people/mgr-1/org-chart", params={"direction": "down", "depth": 1},
        headers=auth_headers("hr"),
    )
    assert resp.status_code == 200
    nodes = resp.json()
    drew = next(n for n in nodes if n["id"] == "delegates-to-restricted-1")
    assert drew["delegate"]["id"] == "restricted-1"


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


# ---------------------------------------------------------------------------
# linkedin_profile. Readable by every role (a LinkedIn page is already
# public, so it sits in BASE_FIELDS, not INTERNAL_FIELDS); editable only by
# HR in work mode, like every other update_employee field.
#
# The migration is the part worth guarding. Six PRs added this column to the
# model, schemas, permissions, registry and the React form with NO migration,
# and the whole suite passed — because tests build their schema from the
# models with create_all(), so SQLite always has the column while the
# deployed database does not. test_sql_portability.py covers the general
# class; this covers the specific column.
# ---------------------------------------------------------------------------

def test_linkedin_profile_has_a_migration():
    """A model column with no migration passes every test and 500s the
    deployed app on the first employee query."""
    import re
    from pathlib import Path

    versions = Path(__file__).resolve().parent.parent / "alembic" / "versions"
    adds = [
        f.name for f in versions.glob("*.py")
        if re.search(r"add_column\(\s*[\"']employees[\"']\s*,\s*sa\.Column\(\s*[\"']linkedin_profile[\"']",
                     f.read_text())
    ]
    assert adds, "employees.linkedin_profile exists on the model but no migration adds it"


async def test_linkedin_profile_is_visible_to_every_role(client, db_session):
    from app.models import Employee

    target = db_session.query(Employee).filter(Employee.id == "stranger-1").one()
    target.linkedin_profile = "https://www.linkedin.com/in/test-person-abc123"
    db_session.commit()
    try:
        for role in ("employee", "manager", "hr"):
            resp = await client.get("/people/stranger-1", headers=auth_headers(role))
            assert resp.status_code == 200
            assert resp.json()["linkedin_profile"] == "https://www.linkedin.com/in/test-person-abc123", role
    finally:
        target.linkedin_profile = None
        db_session.commit()


async def test_linkedin_profile_is_null_not_absent_when_unset(client, db_session):
    """Present-as-null, matching bio and away_until_month.

    The absent-never-null rule in this codebase is about VISIBILITY: a field
    the caller may not see is left unset and dropped by exclude_unset, so its
    key is genuinely missing. linkedin_profile is in BASE_FIELDS, so every
    caller may see it — _build_detail therefore always sets it, and a person
    with no URL on file comes back as null rather than absent. Null here
    means "no URL recorded", which is information the caller is entitled to;
    an absent key would wrongly imply a permission boundary.
    """
    from app.models import Employee

    target = db_session.query(Employee).filter(Employee.id == "stranger-1").one()
    assert target.linkedin_profile is None
    body = (await client.get("/people/stranger-1", headers=auth_headers("hr"))).json()
    assert "linkedin_profile" in body
    assert body["linkedin_profile"] is None
    # the contrast that makes the distinction real: a genuinely gated field
    # is ABSENT for a caller who can't see it, not null.
    employee_body = (await client.get("/people/stranger-1", headers=auth_headers("employee"))).json()
    assert "salary" not in employee_body
