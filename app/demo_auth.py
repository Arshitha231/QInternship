"""Demo credentials for the dev auth provider — NOT production authentication.

There is no password column on Employee, no hashing, and no reset flow. This
module maps a small fixed set of seeded work emails to directory roles and
checks them against one shared password read from the environment. It exists
so the demo can be driven from a login form instead of a role dropdown, and
so the four roles can be shown off without hand-editing request headers.

It refuses to run outside dev mode (see auth_mode). Configure Entra and every
route below stops accepting demo logins, because get_current_user stops
reading the dev header the login produces — the shim cannot become the
production door by accident.

Why the account map is keyed by EMAIL and not by employee id: seed.py draws
names from a fixed RNG seed but ids from uuid4, so the local sqlite file and
the deployed Azure SQL database hold the same people under different ids.
Emails are derived from names and are identical in both. Resolving the id
from work_email at login time is what lets one credential list work against
whichever database the API is actually pointed at — see the history in
frontend/src/identities.ts for what the id-keyed version cost us.

Role is not a property of the employee row and is deliberately not stored as
one (app/auth.py explains why). It is a per-request claim, and this map is
the demo's stand-in for the Entra app-role assignment that will carry it in
production.
"""
from __future__ import annotations

import hmac
import os

from dotenv import load_dotenv
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import AuthenticatedUser, Role, auth_mode
from app.models.employee import Employee

load_dotenv()

DEFAULT_DEMO_PASSWORD = "orghub2026"

# The roles here mirror what the identity picker offered before the login
# page replaced it, so the demo tells the same story: one of each privileged
# role, one manager with real direct reports, and plain ICs.
#
# Xiomara -> Sean -> Min-jun is one reporting chain across three accounts,
# which is what makes the certification notifications demoable end to end: a
# status change on Xiomara puts a reminder in her bell and a report in both
# of theirs. Their job titles say "manager" and "VP" while their role here
# says "employee" — that is the point, not an oversight. The directory role
# is an access claim, not a rung on the org chart.
DEMO_ACCOUNTS: dict[str, Role] = {
    "naomi.lewis@example.com": "hr",
    "shaun.iyer@example.com": "it",
    "sean.wilson@example.com": "manager",
    "joshua.liu@example.com": "employee",
    "xiomara.mensah@example.com": "employee",
    "minjun.sanchez@example.com": "employee",
}


class DemoLoginDisabled(Exception):
    """Raised outside dev mode. The route turns this into a 404 rather than a
    403: when real auth is configured, this endpoint does not exist."""


class DemoLoginDenied(Exception):
    """Unknown email or wrong password. Deliberately one exception for both,
    so the response can't be used to tell which of the two it was."""


class DemoAccountNotSeeded(Exception):
    """The credentials are valid but no employee in the connected database
    has that work email — i.e. this database was seeded without them, or
    that person has been deactivated.

    Separated from DemoLoginDenied because it is an operational signal, not
    a failed login: it is what a re-seeded or freshly provisioned database
    looks like from the login form, and saying so plainly is the difference
    between a two-minute fix and a confusing demo.
    """


def demo_password() -> str:
    """Read through a function, not a module constant, so a test can set the
    env var without reimporting the module — same convention as app/config.py."""
    return os.environ.get("DEMO_LOGIN_PASSWORD", DEFAULT_DEMO_PASSWORD)


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


def login(db: Session, email: str, password: str) -> AuthenticatedUser:
    """Resolve demo credentials to the same AuthenticatedUser every other
    provider produces. Nothing downstream can tell how the caller arrived."""
    if auth_mode() != "dev":
        raise DemoLoginDisabled

    normalized = email.strip().lower()
    role = DEMO_ACCOUNTS.get(normalized)
    # compare_digest on both branches so an unknown email and a wrong
    # password take the same path; there is nothing secret to protect here,
    # but writing the check the other way invites copying it somewhere there is.
    supplied_ok = hmac.compare_digest(password, demo_password())
    if role is None or not supplied_ok:
        raise DemoLoginDenied

    employee = _lookup_active_employee(db, normalized)
    if employee is None:
        raise DemoAccountNotSeeded(normalized)

    return AuthenticatedUser(
        id=employee.id,
        role=role,
        name=employee.full_name,
        email=employee.work_email,
    )
