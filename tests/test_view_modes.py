"""View modes: the (field, role, view_mode) visibility table, end to end.

Same rules as test_visibility.py — replay real HTTP requests, assert on
response bodies, never inspect service internals. The one exception is
test_employee_mode_table_is_role_independent, which asserts a property of
the config table itself; it's the structural guarantee that the HTTP-level
identity test below is checking the consequence of.
"""
import pytest

from app.permissions import ALLOWED, VALID_VIEW_MODES, resolve_view_mode
from tests.conftest import auth_headers

ALL_ROLES = ["employee", "manager", "hr", "it"]
INTERNAL = ("salary", "salary_currency", "date_of_birth", "hire_date", "cost_centre")


# ---------------------------------------------------------------------------
# The it role exists and authenticates.
# ---------------------------------------------------------------------------

async def test_it_role_authenticates(client):
    resp = await client.get("/auth/whoami", headers=auth_headers("it"))
    assert resp.status_code == 200
    assert resp.json()["role"] == "it"


async def test_unknown_role_still_rejected(client):
    """Adding a fourth role must not turn the role check into a pass-through."""
    resp = await client.get("/auth/whoami", headers=auth_headers("admin"))
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Internal info: hr in work mode only. Not it, in either mode.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("role,view_mode,expected_present", [
    ("hr", "work", True),
    ("hr", "employee", False),
    ("it", "work", False),      # the headline case: IT is privileged, not paid-privileged
    ("it", "employee", False),
    ("employee", "work", False),
    ("manager", "work", False),
])
async def test_internal_fields_by_role_and_view_mode(client, role, view_mode, expected_present):
    resp = await client.get(
        "/people/stranger-1", params={"view_mode": view_mode}, headers=auth_headers(role)
    )
    assert resp.status_code == 200
    body = resp.json()
    for field in INTERNAL:
        assert (field in body) is expected_present, (
            f"role={role} view_mode={view_mode}: expected '{field}' "
            f"present={expected_present}, got body={body}"
        )


async def test_it_cannot_see_salary_but_hr_can(client):
    """The requirement stated on its own, in one place, so a regression in
    the table reads as this failure rather than as a parametrize case."""
    it_body = (await client.get(
        "/people/stranger-1", params={"view_mode": "work"}, headers=auth_headers("it"))).json()
    hr_body = (await client.get(
        "/people/stranger-1", params={"view_mode": "work"}, headers=auth_headers("hr"))).json()

    assert "salary" not in it_body
    assert hr_body["salary"] == "95000.00"


# ---------------------------------------------------------------------------
# project_desc: visible to every role in every view_mode — moved into
# BASE_FIELDS so "available to all employees" holds in the mode that
# literally represents what an employee sees. Editable by it, work mode
# only (edit side lives in the write-endpoint tests) — visibility widened,
# editing did not.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("role", ["employee", "manager", "hr", "it"])
@pytest.mark.parametrize("view_mode", ["employee", "work"])
async def test_project_desc_visible_to_everyone(client, role, view_mode):
    resp = await client.get(
        "/people/stranger-1", params={"view_mode": view_mode}, headers=auth_headers(role)
    )
    assert resp.status_code == 200
    atlas = next(p for p in resp.json()["project_history"] if p["project_name"] == "Project Atlas")
    assert "project_desc" in atlas, f"role={role} mode={view_mode}: {atlas}"
    assert atlas["project_desc"].startswith("Internal migration")


async def test_project_desc_not_editable_by_a_role_that_can_now_see_it():
    """Visibility widened; editing did not — a plain employee can now read
    project_desc but still may not write it. Full HTTP coverage of the
    write side lives in test_write_endpoints.py; this pins the config-table
    invariant the widening must not have disturbed."""
    from app.permissions import can_edit

    assert can_edit("employee", "employee", "project_desc") is False
    assert can_edit("employee", "work", "project_desc") is False
    assert can_edit("hr", "work", "project_desc") is False
    assert can_edit("it", "work", "project_desc") is True


# ---------------------------------------------------------------------------
# The identity guarantee: employee-mode output does not depend on the
# caller's role. Same caller id throughout, so ABAC — which keys on identity,
# not role — is held constant and what's left varying is only the role.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("target", ["stranger-1", "report-1", "member-1"])
async def test_employee_mode_output_identical_across_roles(client, target):
    bodies = {}
    for role in ALL_ROLES:
        resp = await client.get(
            f"/people/{target}", params={"view_mode": "employee"},
            headers=auth_headers(role, "caller-x"),
        )
        assert resp.status_code == 200
        bodies[role] = resp.json()

    baseline = bodies["employee"]
    for role in ALL_ROLES[1:]:
        assert bodies[role] == baseline, (
            f"employee-mode output differs for role={role}\n"
            f"  employee: {baseline}\n  {role}: {bodies[role]}"
        )


async def test_employee_mode_list_identical_across_roles(client):
    """Same guarantee on the list endpoint, not just the detail one."""
    bodies = []
    for role in ALL_ROLES:
        resp = await client.get(
            "/people", params={"name": "Sam Stranger", "view_mode": "employee"},
            headers=auth_headers(role, "caller-x"),
        )
        assert resp.status_code == 200
        bodies.append(resp.json())
    assert all(b == bodies[0] for b in bodies), bodies


async def test_hr_loses_restricted_record_access_in_employee_mode(client):
    """The sharp edge of the identity guarantee: HR's restricted-record
    exemption is a role privilege, so employee mode drops it too. Without
    this, employee-mode output would silently differ for HR."""
    work = await client.get(
        "/people/restricted-1", params={"view_mode": "work"}, headers=auth_headers("hr"))
    employee = await client.get(
        "/people/restricted-1", params={"view_mode": "employee"}, headers=auth_headers("hr"))

    assert work.status_code == 200
    assert employee.status_code == 404


async def test_own_salary_still_visible_in_employee_mode(client):
    """Self-access is ABAC, not a role privilege, so it survives employee
    mode — an hr caller looking at their own profile in employee mode sees
    their own salary, exactly as any employee would."""
    for role in ALL_ROLES:
        resp = await client.get(
            "/people/stranger-1", params={"view_mode": "employee"},
            headers=auth_headers(role, "stranger-1"),
        )
        assert resp.status_code == 200
        assert resp.json()["salary"] == "95000.00", f"role={role}"


# ---------------------------------------------------------------------------
# Server-side forcing: the client's parameter never widens the view.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("role", ["employee", "manager"])
async def test_work_mode_is_forced_to_employee_for_unprivileged_roles(client, role):
    """Asking for work mode directly, bypassing any UI, changes nothing."""
    forced = await client.get(
        "/people/stranger-1", params={"view_mode": "work"}, headers=auth_headers(role))
    natural = await client.get(
        "/people/stranger-1", params={"view_mode": "employee"}, headers=auth_headers(role))

    assert forced.json() == natural.json()
    for field in INTERNAL:
        assert field not in forced.json()


@pytest.mark.parametrize("role,requested,expected", [
    ("employee", "work", "employee"),
    ("manager", "work", "employee"),
    ("employee", None, "employee"),
    ("hr", "work", "work"),
    ("hr", "employee", "employee"),
    ("it", "employee", "employee"),
    # Unset: hr/it keep the full view they had before view modes existed.
    ("hr", None, "work"),
    ("it", None, "work"),
    # Garbage narrows, never widens — and never 400s.
    ("hr", "administrator", "work"),
    ("employee", "administrator", "employee"),
])
def test_resolve_view_mode(role, requested, expected):
    assert resolve_view_mode(role, requested) == expected


# ---------------------------------------------------------------------------
# Config-table properties. Not HTTP, deliberately: these are the invariants
# the endpoint tests above are downstream of.
# ---------------------------------------------------------------------------

def test_employee_mode_table_is_role_independent():
    rows = {role: ALLOWED[(role, "employee")] for role in ALL_ROLES}
    assert all(fields == rows["employee"] for fields in rows.values()), rows


def test_every_role_view_mode_pair_is_in_the_table():
    """Deny by default means a missing key is a KeyError at request time,
    not an empty-and-therefore-safe lookup. Assert the table is total."""
    for role in ALL_ROLES:
        for mode in VALID_VIEW_MODES:
            assert (role, mode) in ALLOWED, f"({role}, {mode}) missing from ALLOWED"


def test_no_role_sees_internal_fields_in_employee_mode():
    for role in ALL_ROLES:
        assert not (ALLOWED[(role, "employee")] & set(INTERNAL)), role
