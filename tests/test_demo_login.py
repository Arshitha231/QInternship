"""Demo login (POST /auth/login).

The account map is monkeypatched onto the conftest fixture people in every
test here. Pointing these at the real seeded names (naomi.lewis@..., ...)
would tie the suite to seed.py having been run, which is exactly the
coupling app/demo_auth.py exists to avoid — it resolves ids from whatever
database is connected, and the test database is a five-person fixture set.
"""
from datetime import date

import pytest

from app.models import Employee, Office, OrgUnit
from app.models.enums import AvailabilityStatus, EmploymentType
from tests.conftest import auth_headers

FIXTURE_ACCOUNTS = {
    "morgan@example.test": "manager",
    "riley@example.test": "employee",
    "sam@example.test": "hr",
}


@pytest.fixture(autouse=True)
def _accounts(monkeypatch):
    monkeypatch.setattr("app.demo_auth.DEMO_ACCOUNTS", dict(FIXTURE_ACCOUNTS))
    monkeypatch.setenv("DEMO_LOGIN_PASSWORD", "test-password")


async def _login(client, email, password="test-password"):
    return await client.post("/auth/login", json={"email": email, "password": password})


async def test_login_returns_the_id_from_the_connected_database(client):
    res = await _login(client, "morgan@example.test")
    assert res.status_code == 200
    assert res.json() == {
        "id": "mgr-1",
        "role": "manager",
        "name": "Morgan Manager",
        "email": "morgan@example.test",
    }


async def test_role_comes_from_the_account_map_not_the_org_tree(client):
    # Sam Stranger is a Financial Analyst who manages nobody; the map says
    # hr and the map is what decides. Role is a claim, not a job title.
    res = await _login(client, "sam@example.test")
    assert res.status_code == 200
    assert res.json()["role"] == "hr"


async def test_login_is_case_and_whitespace_insensitive(client):
    res = await _login(client, "  MORGAN@Example.TEST ")
    assert res.status_code == 200
    assert res.json()["id"] == "mgr-1"


async def test_wrong_password_is_401(client):
    res = await _login(client, "morgan@example.test", password="not-it")
    assert res.status_code == 401


async def test_unknown_email_is_401_with_the_same_message_as_a_wrong_password(client):
    unknown = await _login(client, "nobody@example.test")
    wrong = await _login(client, "morgan@example.test", password="not-it")
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json()["detail"] == wrong.json()["detail"]


async def test_valid_account_missing_from_this_database_is_503_not_401(client, monkeypatch):
    """A re-seeded or fresh database, seen from the login form. The password
    was right; saying '401' here would send someone hunting for a typo."""
    monkeypatch.setattr(
        "app.demo_auth.DEMO_ACCOUNTS", {"never.seeded@example.test": "employee"})
    res = await _login(client, "never.seeded@example.test")
    assert res.status_code == 503
    assert "re-seeding" in res.json()["detail"]


async def test_deactivated_employee_cannot_log_in(client, db_session, monkeypatch):
    # A throwaway employee, never one of conftest's shared fixture people —
    # the test database is session-scoped, so mutating a shared one would
    # leak into every test that runs after this.
    gone = Employee(
        id="login-deactivated-1", full_name="Dana Departed", job_title="Software Engineer",
        work_email="dana.departed@example.test",
        org_unit_id=db_session.query(OrgUnit).filter(OrgUnit.name == "Platform Engineering").one().id,
        office_id=db_session.query(Office).filter(Office.name == "Test HQ").one().id,
        manager_id=None, employment_type=EmploymentType.fte, hire_date=date(2021, 1, 1),
        availability_status=AvailabilityStatus.available, is_active=False,
    )
    db_session.add(gone)
    db_session.commit()
    try:
        monkeypatch.setattr(
            "app.demo_auth.DEMO_ACCOUNTS", {"dana.departed@example.test": "employee"})
        res = await _login(client, "dana.departed@example.test")
        assert res.status_code == 503
    finally:
        db_session.delete(gone)
        db_session.commit()


async def test_the_login_response_authenticates_real_routes(client):
    """The whole contract: what login hands back is what the headers carry,
    and get_current_user can't tell the difference."""
    identity = (await _login(client, "morgan@example.test")).json()
    res = await client.get("/auth/whoami", headers={
        "X-Dev-Role": identity["role"],
        "X-Dev-User-Id": identity["id"],
        "X-Dev-Name": identity["name"],
    })
    assert res.status_code == 200
    assert res.json()["id"] == "mgr-1"
    assert res.json()["role"] == "manager"


async def test_login_404s_once_real_auth_is_configured(client, monkeypatch):
    """Not 403 — outside dev mode this endpoint does not exist at all, because
    signing in is then Entra's job and these credentials are not an
    alternative route to the same thing."""
    monkeypatch.setenv("AUTH_MODE", "entra")
    res = await _login(client, "morgan@example.test")
    assert res.status_code == 404


async def test_dev_headers_still_work_without_logging_in(client):
    """Login is an addition, not a replacement: every existing test drives
    the API by header alone and must keep doing so."""
    res = await client.get("/auth/whoami", headers=auth_headers("hr"))
    assert res.status_code == 200
