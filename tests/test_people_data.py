"""Salary / date of birth visibility, and the date-driven notification sweep.

Fixtures build their own org unit and people and remove them again — the rest
of the suite shares one session-scoped database and assumes it stays as
conftest seeded it.
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.auth import AuthenticatedUser
from app.models import AuditLog, Employee, Notification, OrgUnit
from app.models.enums import EmploymentType, is_milestone_year
from app.notifications import NotifyDateMilestonesDenied, _falls_on, hr_recipients, notify_date_milestones
from tests.conftest import auth_headers

TODAY = date.today()

# notify_date_milestones enforces (role == "hr") itself now, same as
# record_course_status — direct-call tests below stand in for the route (or
# a future scheduler), so they pass a caller the same way the route's own
# auth dependency would.
_HR_CALLER = AuthenticatedUser(id="hr-caller-test", role="hr")

HR_ONE = "hrdata-hr-1"
HR_TWO = "hrdata-hr-2"
BIRTHDAY_PERSON = "hrdata-birthday"
ANNIVERSARY_PERSON = "hrdata-anniversary"
NO_DOB_PERSON = "hrdata-no-dob"
HR_WITH_BIRTHDAY = "hrdata-hr-birthday"


def _years_ago(years: int) -> date:
    try:
        return TODAY.replace(year=TODAY.year - years)
    except ValueError:  # 29 Feb
        return TODAY.replace(year=TODAY.year - years, day=28)


@pytest.fixture(scope="module", autouse=True)
def people_data_fixtures():
    from app.db import SessionLocal

    db = SessionLocal()
    created_ids = [HR_ONE, HR_TWO, BIRTHDAY_PERSON, ANNIVERSARY_PERSON,
                   NO_DOB_PERSON, HR_WITH_BIRTHDAY]
    try:
        company = db.query(OrgUnit).filter(OrgUnit.name == "TestCo").one()
        # Named exactly as config.hr_org_unit_name() defaults, since that's
        # what the sweep resolves recipients from.
        hr_unit = OrgUnit(name="HR Operations", parent_id=company.id, unit_type="department")
        db.add(hr_unit)
        db.flush()

        eng = db.query(OrgUnit).filter(OrgUnit.name == "Platform Engineering").one()

        def mk(id_, name, unit_id, **kw):
            fields = dict(
                id=id_, full_name=name, job_title="Specialist", org_unit_id=unit_id,
                work_email=f"{id_}@example.test", employment_type=EmploymentType.fte,
                hire_date=date(2021, 3, 4), is_active=True,
            )
            fields.update(kw)
            db.add(Employee(**fields))

        mk(HR_ONE, "Hana Resource", hr_unit.id)
        mk(HR_TWO, "Hugo Resource", hr_unit.id)
        # An HR person whose own birthday is today — they must not be told
        # about themselves, but their colleague must be told about them.
        mk(HR_WITH_BIRTHDAY, "Hilda Resource", hr_unit.id, date_of_birth=_years_ago(40))
        mk(BIRTHDAY_PERSON, "Bertie Birthday", eng.id, date_of_birth=_years_ago(33))
        # Exactly five years of service today.
        mk(ANNIVERSARY_PERSON, "Anna Anniversary", eng.id, hire_date=_years_ago(5),
           date_of_birth=_years_ago(41))
        # No date of birth on file — must be skipped silently.
        mk(NO_DOB_PERSON, "Dana Nodob", eng.id, date_of_birth=None)

        # Salary/DOB on an employee who already has a manager, so the
        # "manager must not see these" case has a real relationship to test.
        report = db.get(Employee, "report-1")
        report.salary = Decimal("125000.00")
        report.salary_currency = "USD"
        report.date_of_birth = date(1990, 6, 15)
        db.commit()
        yield
    finally:
        report = db.get(Employee, "report-1")
        report.salary = None
        report.salary_currency = None
        report.date_of_birth = None
        db.query(Notification).delete()
        db.query(AuditLog).filter(AuditLog.action == "notify_date_milestones").delete()
        db.query(Employee).filter(Employee.id.in_(created_ids)).delete(synchronize_session=False)
        db.query(OrgUnit).filter(OrgUnit.name == "HR Operations").delete()
        db.commit()
        db.close()


@pytest.fixture(autouse=True)
def clean_notifications(db_session):
    db_session.query(Notification).delete()
    db_session.commit()
    yield


# ---------------------------------------------------------------------------
# Visibility: HR and the person, and nobody else — not even the manager.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("field", ["salary", "salary_currency", "date_of_birth"])
@pytest.mark.parametrize("role,caller_id,expected,case", [
    ("employee", "report-1", True, "own profile"),
    ("hr", "stranger-1", True, "hr, no relationship"),
    ("manager", "mgr-1", False, "DIRECT MANAGER — deliberately excluded"),
    ("employee", "mgr-1", False, "direct manager, employee role"),
    ("employee", "stranger-1", False, "unrelated colleague"),
])
async def test_salary_and_dob_visibility(client, field, role, caller_id, expected, case):
    resp = await client.get("/people/report-1", headers=auth_headers(role, caller_id))
    assert resp.status_code == 200
    assert (field in resp.json()) is expected, f"{field} / {case}: {resp.json()}"


async def test_manager_still_sees_personal_mobile(client):
    """The narrower salary/DOB rule must not have tightened personal_mobile,
    which is own-profile OR direct manager by design."""
    resp = await client.get("/people/report-1", headers=auth_headers("manager", "mgr-1"))
    body = resp.json()
    assert "personal_mobile" in body
    assert "salary" not in body


async def test_salary_null_when_not_held_but_visible(client):
    """No salary on file (a contractor, say) is null — NOT absent.

    Absent has to keep meaning "you may not see this". If an empty salary also
    vanished from the response, HR couldn't tell "we hold no salary for this
    person" from "this field is hidden from you", which is the exact ambiguity
    the absent-vs-null convention exists to prevent.
    """
    resp = await client.get(f"/people/{BIRTHDAY_PERSON}",
                            headers=auth_headers("hr", "stranger-1"))
    assert resp.status_code == 200
    body = resp.json()
    assert "salary" in body
    assert body["salary"] is None
    # ...and it really is hidden, not merely empty, from someone unentitled.
    other = await client.get(f"/people/{BIRTHDAY_PERSON}",
                             headers=auth_headers("employee", "stranger-1"))
    assert "salary" not in other.json()


async def test_salary_serialized_exactly(client):
    resp = await client.get("/people/report-1", headers=auth_headers("employee", "report-1"))
    assert resp.json()["salary"] == "125000.00"  # a string, so no float rounding


# ---------------------------------------------------------------------------
# Date arithmetic.
# ---------------------------------------------------------------------------

def test_milestone_years():
    assert [y for y in range(0, 41) if is_milestone_year(y)] == [1, 5, 10, 15, 20, 25, 30, 35, 40]


@pytest.mark.parametrize("born,on,expected,case", [
    (date(2000, 2, 29), date(2026, 2, 28), True, "leap-day birthday observed on 28 Feb in a non-leap year"),
    (date(2000, 2, 29), date(2028, 2, 28), False, "not observed early when the 29th exists"),
    (date(2000, 2, 29), date(2028, 2, 29), True, "falls on the real day in a leap year"),
    (date(1990, 8, 13), date(2026, 8, 13), True, "ordinary match"),
    (date(1990, 8, 14), date(2026, 8, 13), False, "ordinary miss"),
])
def test_falls_on(born, on, expected, case):
    assert _falls_on(born, on) is expected, case


# ---------------------------------------------------------------------------
# The sweep.
# ---------------------------------------------------------------------------

def test_hr_recipients_resolved_from_the_org_tree(db_session):
    ids = {e.id for e in hr_recipients(db_session)}
    assert {HR_ONE, HR_TWO, HR_WITH_BIRTHDAY} <= ids
    assert "report-1" not in ids  # engineering, not HR


def test_sweep_notifies_hr_about_birthdays_and_anniversaries(db_session):
    created = notify_date_milestones(db_session, _HR_CALLER, TODAY)
    db_session.commit()

    by_recipient = {}
    for n in created:
        by_recipient.setdefault(n.recipient_id, []).append(n.body)

    assert HR_ONE in by_recipient and HR_TWO in by_recipient
    bodies = by_recipient[HR_ONE]
    assert any("Bertie Birthday" in b and "birthday" in b for b in bodies)
    assert any("Anna Anniversary" in b and "5 years" in b for b in bodies)
    # No date of birth on file -> silently skipped, not guessed at.
    assert not any("Dana Nodob" in b for b in bodies)
    # Nobody outside HR is told.
    assert all(r in {HR_ONE, HR_TWO, HR_WITH_BIRTHDAY} for r in by_recipient)


def test_birthday_message_reveals_neither_age_nor_date(db_session):
    created = notify_date_milestones(db_session, _HR_CALLER, TODAY)
    db_session.commit()
    birthday_bodies = [n.body for n in created if n.kind.value == "birthday_reminder"]
    assert birthday_bodies
    for body in birthday_bodies:
        assert str(TODAY.year) not in body
        assert "33" not in body  # Bertie's age
        assert "-" not in body   # no ISO date slipped in


def test_hr_person_is_not_told_about_their_own_birthday(db_session):
    created = notify_date_milestones(db_session, _HR_CALLER, TODAY)
    db_session.commit()
    own = [n for n in created
           if n.recipient_id == HR_WITH_BIRTHDAY and n.subject_employee_id == HR_WITH_BIRTHDAY]
    assert own == []
    # ...but their colleagues still hear about it, so the day isn't missed.
    assert any(n.recipient_id == HR_ONE and n.subject_employee_id == HR_WITH_BIRTHDAY
               for n in created)


def test_sweep_is_idempotent(db_session):
    first = notify_date_milestones(db_session, _HR_CALLER, TODAY)
    db_session.commit()
    assert first
    second = notify_date_milestones(db_session, _HR_CALLER, TODAY)
    db_session.commit()
    assert second == [], "a re-run must not produce a second birthday message"


def test_sweep_on_a_quiet_date_notifies_nobody(db_session):
    quiet = TODAY + timedelta(days=1)
    if any(_falls_on(d, quiet) for d in (_years_ago(33), _years_ago(5), _years_ago(40))):
        pytest.skip("tomorrow happens to be a seeded milestone")
    assert notify_date_milestones(db_session, _HR_CALLER, quiet) == []


def test_sweep_writes_an_audit_entry(db_session):
    before = db_session.query(AuditLog).filter(AuditLog.action == "notify_date_milestones").count()
    notify_date_milestones(db_session, _HR_CALLER, TODAY)
    db_session.commit()
    after = db_session.query(AuditLog).filter(AuditLog.action == "notify_date_milestones").count()
    assert after == before + 1


def test_service_denies_a_non_hr_caller_even_without_a_route(db_session):
    """The route already blocks this, but the service must too — a future
    scheduler calling this directly gets the same refusal unless it
    constructs an hr-role caller identity of its own."""
    non_hr = AuthenticatedUser(id="manager-caller-test", role="manager")
    with pytest.raises(NotifyDateMilestonesDenied):
        notify_date_milestones(db_session, non_hr, TODAY)


# ---------------------------------------------------------------------------
# Route.
# ---------------------------------------------------------------------------

async def test_date_sweep_route_is_hr_only(client):
    resp = await client.post("/notifications/date-milestones",
                             headers=auth_headers("manager", "mgr-1"))
    assert resp.status_code == 403


async def test_date_sweep_route_runs_and_is_idempotent(client):
    first = await client.post(f"/notifications/date-milestones?on={TODAY.isoformat()}",
                              headers=auth_headers("hr", "stranger-1"))
    assert first.status_code == 201
    body = first.json()
    assert body["notifications_sent"] > 0
    assert set(body["by_kind"]) == {"birthday_reminder", "work_anniversary_reminder"}

    second = await client.post(f"/notifications/date-milestones?on={TODAY.isoformat()}",
                               headers=auth_headers("hr", "stranger-1"))
    assert second.json()["notifications_sent"] == 0
