"""Demo credentials for the dev auth provider — NOT production authentication.

There is no password column on Employee, no hashing, and no reset flow. Any
active employee can sign in with their work email and one shared password
read from the environment. It exists so the demo can be driven from a login
form instead of a role dropdown, and so any person in the directory can be
signed in as on request rather than only a curated handful.

It refuses to run outside dev mode (see auth_mode). Configure Entra and the
route disappears, because get_current_user stops reading the dev header this
produces — the shim cannot become the production door by accident. Nothing
is given away by the "anyone can sign in" rule that dev mode did not already
give away: there, sending `X-Dev-Role: hr` by hand has always been enough.

Sign-in is by EMAIL, and the employee id is looked up at request time. seed.py
draws names from a fixed RNG seed but ids from uuid4, so the local sqlite file
and the deployed Azure SQL database hold the same people under different ids.
Emails are derived from names and are identical in both. Resolving the id from
work_email is what lets one set of credentials work against whichever database
the API is pointed at — see the history in the file this replaced
(frontend/src/identities.ts) for what the id-keyed version cost us.
"""
from __future__ import annotations

import hmac
import os

from dotenv import load_dotenv
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import config
from app.auth import AuthenticatedUser, Role, auth_mode
from app.models.employee import Employee

load_dotenv()

DEFAULT_DEMO_PASSWORD = "orghub2026"
DEFAULT_IT_ORG_UNIT = "IT"


class DemoLoginDisabled(Exception):
    """Raised outside dev mode. The route turns this into a 404 rather than a
    403: when real auth is configured, this endpoint does not exist."""


class DemoLoginDenied(Exception):
    """Unknown email, wrong password, or a deactivated employee. Deliberately
    one exception for all three, so the response can't be used to tell which."""


def demo_password() -> str:
    """Read through a function, not a module constant, so a test can set the
    env var without reimporting the module — same convention as app/config.py."""
    return os.environ.get("DEMO_LOGIN_PASSWORD", DEFAULT_DEMO_PASSWORD)


def it_org_unit_name() -> str:
    """Which org unit's people are treated as IT when deriving a role.

    The mirror of config.hr_org_unit_name(), and a setting for the same
    reason: the unit's name is a fact about one company's org chart, not
    about this code. Lives here rather than in app/config.py because only
    this demo shim consults it — the real IT role will arrive as an Entra
    app-role claim and will not consult the org tree at all.
    """
    return os.environ.get("IT_ORG_UNIT_NAME", DEFAULT_IT_ORG_UNIT).strip() or DEFAULT_IT_ORG_UNIT


def _lookup_active_employee(db: Session, email: str) -> Employee | None:
    # Case-insensitive on the stored column as well as the input: seeded
    # emails are lowercase today, but nothing enforces that, and a demo
    # login failing over a capital letter is a bad thirty seconds on stage.
    return db.execute(
        select(Employee).where(
            func.lower(Employee.work_email) == email,
            # `== True`, not `.is_(True)`: the latter renders as `IS 1`, which
            # Azure SQL rejects (tests/test_sql_portability.py enforces this).
            Employee.is_active == True,  # noqa: E712
        )
    ).scalar_one_or_none()


def _in_org_subtree(db: Session, employee: Employee, unit_name: str) -> bool:
    from app.people import _org_unit_and_descendant_ids

    if employee.org_unit_id is None:
        return False
    unit_ids = _org_unit_and_descendant_ids(db, unit_name)
    return bool(unit_ids) and employee.org_unit_id in unit_ids


def _has_active_direct_reports(db: Session, employee_id: str) -> bool:
    return db.execute(
        select(Employee.id).where(
            Employee.manager_id == employee_id,
            Employee.is_active == True,  # noqa: E712
        ).limit(1)
    ).first() is not None


def derive_role(db: Session, employee: Employee) -> Role:
    """Which directory role this person signs in with.

    Role is not a column on Employee and deliberately never will be: it is a
    per-request claim that production takes from an Entra app-role assignment
    (app/auth.py). This shim has no claim to read, so it falls back to the
    org tree — the same signal, and the same justification, as
    config.hr_org_unit_name() already uses to decide who counts as HR for
    notifications in a scheduled sweep that likewise has no request behind it.

    The rules, in order:

      it       — anyone in the IT division or below it
      hr       — anyone in the HR unit or below it
      manager  — anyone with at least one ACTIVE direct report
      employee — everyone else

    IT and HR outrank manager because directors in those units manage people
    too, and being HR's director is the more specific fact about them. Reports
    are counted only while active, so managing one person who has since been
    deactivated does not leave someone holding a manager's view of an empty team.
    """
    if _in_org_subtree(db, employee, it_org_unit_name()):
        return "it"
    if _in_org_subtree(db, employee, config.hr_org_unit_name()):
        return "hr"
    if _has_active_direct_reports(db, employee.id):
        return "manager"
    return "employee"


def login(db: Session, email: str, password: str) -> AuthenticatedUser:
    """Resolve credentials to the same AuthenticatedUser every other provider
    produces. Nothing downstream can tell how the caller arrived."""
    if auth_mode() != "dev":
        raise DemoLoginDisabled

    # Checked before the lookup and on every path, so a wrong password and an
    # unknown email cost the same and are reported the same. There is nothing
    # secret here to protect, but writing the check the other way invites
    # copying it somewhere there is.
    supplied_ok = hmac.compare_digest(password, demo_password())
    employee = _lookup_active_employee(db, email.strip().lower())
    if not supplied_ok or employee is None:
        raise DemoLoginDenied

    return AuthenticatedUser(
        id=employee.id,
        role=derive_role(db, employee),
        name=employee.full_name,
        email=employee.work_email,
    )
