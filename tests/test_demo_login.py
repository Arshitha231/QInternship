"""Demo login (POST /auth/login).

Any active employee can sign in; the role is derived from the org tree
(app/demo_auth.derive_role). The tests point the HR/IT unit names at units
the conftest fixture set actually has, rather than adding "HR Operations"
and "IT" to it — the fixture database is session-scoped and shared, and new
org units would show up in every other test that lists or filters them.
"""
from datetime import date

import pytest

from app.models import Employee, Office, OrgUnit
from app.models.enums import AvailabilityStatus, EmploymentType
from tests.conftest import auth_headers

PASSWORD = "test-password"


@pytest.fixture(autouse=True)
def _password(monkeypatch):
    monkeypatch.setenv("DEMO_LOGIN_PASSWORD", PASSWORD)
    # Neither fixture unit is HR or IT by default, so every test below starts
    # from "nobody is privileged" and opts in by repointing one name.
    monkeypatch.setenv("HR_ORG_UNIT_NAME", "Nonexistent HR Unit")
    monkeypatch.setenv("IT_ORG_UNIT_NAME", "Nonexistent IT Unit")


async def _login(client, email, password=PASSWORD):
    return await client.post("/auth/login", json={"email": email, "password": password})


def _mkemp(db_session, id_, full_name, **overrides) -> Employee:
    """A throwaway employee dedicated to one test, never one of conftest's
    shared fixture people — the test database is session-scoped, so mutating
    a shared one would leak into every test that runs after this."""
    office = db_session.query(Office).filter(Office.name == "Test HQ").one()
    unit = db_session.query(OrgUnit).filter(OrgUnit.name == "Platform Engineering").one()
    fields = dict(
        id=id_, full_name=full_name, job_title="Test Employee",
        org_unit_id=unit.id, office_id=office.id, manager_id=None,
        work_email=f"{id_}@example.test", employment_type=EmploymentType.fte,
        hire_date=date(2021, 1, 1), availability_status=AvailabilityStatus.available,
        is_active=True,
    )
    fields.update(overrides)
    emp = Employee(**fields)
    db_session.add(emp)
    db_session.commit()
    return emp


# --- who can sign in ------------------------------------------------------

async def test_any_active_employee_can_sign_in(client):
    """Not a curated list: Riley Report is an ordinary fixture person with no
    special standing anywhere in the app."""
    res = await _login(client, "riley@example.test")
    assert res.status_code == 200
    assert res.json()["id"] == "report-1"
    assert res.json()["name"] == "Riley Report"


async def test_the_id_comes_from_the_connected_database(client):
    res = await _login(client, "morgan@example.test")
    assert res.json()["id"] == "mgr-1"


async def test_login_is_case_and_whitespace_insensitive(client):
    res = await _login(client, "  MORGAN@Example.TEST ")
    assert res.status_code == 200
    assert res.json()["id"] == "mgr-1"


async def test_wrong_password_is_401(client):
    assert (await _login(client, "riley@example.test", password="not-it")).status_code == 401


async def test_unknown_email_is_indistinguishable_from_a_wrong_password(client):
    unknown = await _login(client, "nobody@example.test")
    wrong = await _login(client, "riley@example.test", password="not-it")
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json()["detail"] == wrong.json()["detail"]


async def test_deactivated_employee_cannot_sign_in(client, db_session):
    gone = _mkemp(db_session, "login-deactivated-1", "Dana Departed", is_active=False)
    try:
        res = await _login(client, "login-deactivated-1@example.test")
        assert res.status_code == 401
    finally:
        db_session.delete(gone)
        db_session.commit()


# --- what role they get ---------------------------------------------------

async def test_plain_ic_gets_employee(client):
    assert (await _login(client, "riley@example.test")).json()["role"] == "employee"


async def test_managing_someone_active_gets_manager(client):
    """Morgan manages Riley. Nothing on Morgan's own record says "manager"."""
    assert (await _login(client, "morgan@example.test")).json()["role"] == "manager"


async def test_manager_of_only_deactivated_reports_is_not_a_manager(client, db_session):
    boss = _mkemp(db_session, "login-boss-1", "Casey Chief")
    gone = _mkemp(db_session, "login-gone-1", "Dana Departed",
                  manager_id="login-boss-1", is_active=False)
    try:
        res = await _login(client, "login-boss-1@example.test")
        assert res.json()["role"] == "employee"
    finally:
        db_session.delete(gone)
        db_session.delete(boss)
        db_session.commit()


async def test_hr_unit_membership_gets_hr(client, monkeypatch):
    monkeypatch.setenv("HR_ORG_UNIT_NAME", "Finance Operations")
    # Sam Stranger is a Financial Analyst filed under Finance Operations.
    assert (await _login(client, "sam@example.test")).json()["role"] == "hr"


async def test_it_unit_membership_gets_it(client, monkeypatch):
    monkeypatch.setenv("IT_ORG_UNIT_NAME", "Finance Operations")
    assert (await _login(client, "sam@example.test")).json()["role"] == "it"


async def test_it_outranks_hr_when_a_unit_is_named_as_both(client, monkeypatch):
    monkeypatch.setenv("HR_ORG_UNIT_NAME", "Finance Operations")
    monkeypatch.setenv("IT_ORG_UNIT_NAME", "Finance Operations")
    assert (await _login(client, "sam@example.test")).json()["role"] == "it"


async def test_hr_outranks_managing_people(client, monkeypatch):
    """A director in HR manages people too; being HR's is the more specific
    fact, and the one that decides which surfaces they get."""
    monkeypatch.setenv("HR_ORG_UNIT_NAME", "Platform Engineering")
    assert (await _login(client, "morgan@example.test")).json()["role"] == "hr"


async def test_membership_is_inherited_down_the_subtree(client, monkeypatch):
    """Employees are filed under their most specific unit, so naming a
    division has to reach the teams beneath it — the same subtree walk
    org_unit filtering uses. Finance Operations sits under Finance."""
    monkeypatch.setenv("HR_ORG_UNIT_NAME", "Finance")
    assert (await _login(client, "sam@example.test")).json()["role"] == "hr"


async def test_an_unmatched_unit_name_grants_nothing(client, monkeypatch):
    """A renamed or misspelled unit must fail closed, never open."""
    monkeypatch.setenv("HR_ORG_UNIT_NAME", "No Such Unit")
    monkeypatch.setenv("IT_ORG_UNIT_NAME", "No Such Unit Either")
    assert (await _login(client, "sam@example.test")).json()["role"] == "employee"


# --- the contract with the rest of the app --------------------------------

async def test_the_login_response_authenticates_real_routes(client):
    """The whole point: what login hands back is what the headers carry, and
    get_current_user can't tell the difference."""
    identity = (await _login(client, "morgan@example.test")).json()
    res = await client.get("/auth/whoami", headers={
        "X-Dev-Role": identity["role"],
        "X-Dev-User-Id": identity["id"],
        "X-Dev-Name": identity["name"],
    })
    assert res.status_code == 200
    assert res.json() == {"id": "mgr-1", "role": "manager", "name": "Morgan Manager", "email": None}


async def test_login_404s_once_real_auth_is_configured(client, monkeypatch):
    """Not 403 — outside dev mode this endpoint does not exist at all, because
    signing in is then Entra's job and these credentials are not a second way
    to the same thing."""
    monkeypatch.setenv("AUTH_MODE", "entra")
    assert (await _login(client, "morgan@example.test")).status_code == 404


async def test_dev_headers_still_work_without_logging_in(client):
    """Login is an addition, not a replacement: every existing test drives the
    API by header alone and must keep doing so."""
    assert (await client.get("/auth/whoami", headers=auth_headers("hr"))).status_code == 200
