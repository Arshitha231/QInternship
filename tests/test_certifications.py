"""Certification tracking: provider selection, profile visibility, and the
two notification triggers.

Fixtures here create their own courses/requirements/statuses and delete them
again afterwards — the rest of the suite shares one session-scoped database
and assumes it stays as conftest seeded it.
"""
import os
import subprocess
import sys

import pytest

from app.auth import AuthenticatedUser
from app.certifications import RecordCourseStatusDenied, SyntheticCertProvider, get_provider, record_course_status
from app.certifications.base import CertProviderUnavailable, CertStatus
from app.models import (
    CourseRequirement,
    Employee,
    EmployeeCourseStatus,
    Notification,
    OrgUnit,
    TrainingCourse,
)
from app.models.enums import CourseStatus, NotificationKind, display_status
from tests.conftest import auth_headers

# conftest's clean three-level chain: chain-1 -> chain-2 -> chain-3.
SUBJECT = "chain-1"
DIRECT_MANAGER = "chain-2"
SKIP_MANAGER = "chain-3"

# record_course_status enforces (role == "hr") itself now, same as
# update_employee/proposals — direct-call tests below stand in for the
# route, so they pass a caller the same way the route's own auth dependency
# would.
_HR_CALLER = AuthenticatedUser(id="hr-caller-test", role="hr")


@pytest.fixture(scope="module", autouse=True)
def training_fixtures():
    """Three courses: one company-wide, one scoped to the subject's own
    division, one scoped to a division they're not in (so "doesn't apply"
    has something to prove itself against)."""
    from app.db import SessionLocal

    db = SessionLocal()
    try:
        eng = db.query(OrgUnit).filter(OrgUnit.name == "Engineering").one()
        fin = db.query(OrgUnit).filter(OrgUnit.name == "Finance").one()

        courses = {
            "T-ALL": TrainingCourse(code="T-ALL", name="Test Company Wide", is_active=True),
            "T-ENG": TrainingCourse(code="T-ENG", name="Test Engineering Only", is_active=True),
            "T-FIN": TrainingCourse(code="T-FIN", name="Test Finance Only", is_active=True),
        }
        db.add_all(courses.values())
        db.flush()
        db.add_all([
            CourseRequirement(course_id=courses["T-ALL"].id),
            CourseRequirement(course_id=courses["T-ENG"].id, org_unit_id=eng.id),
            CourseRequirement(course_id=courses["T-FIN"].id, org_unit_id=fin.id),
        ])
        db.commit()
        ids = {code: c.id for code, c in courses.items()}
        yield ids
    finally:
        db.query(Notification).delete()
        db.query(EmployeeCourseStatus).delete()
        db.query(CourseRequirement).delete()
        db.query(TrainingCourse).delete()
        db.commit()
        db.close()


@pytest.fixture(autouse=True)
def clean_statuses(db_session):
    """Each test starts with no statuses and no notifications, so a trigger
    assertion is never reading another test's fan-out."""
    db_session.query(Notification).delete()
    db_session.query(EmployeeCourseStatus).delete()
    db_session.commit()
    yield


def _subject(db) -> Employee:
    return db.get(Employee, SUBJECT)


# ---------------------------------------------------------------------------
# display_status: four values in, two out, nothing collapsed in storage.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status,expected", [
    (CourseStatus.completed, "completed"),
    (CourseStatus.not_started, "not_completed"),
    (CourseStatus.in_progress, "not_completed"),
    (CourseStatus.failed, "not_completed"),
])
def test_display_status_derivation(status, expected):
    assert display_status(status).value == expected


def test_underlying_status_survives_a_round_trip(db_session):
    """The label collapses; the stored value must not — the reminder wording
    depends on it."""
    record_course_status(db_session, caller=_HR_CALLER, employee=_subject(db_session),
                         course_code="T-ALL", status=CourseStatus.failed)
    row = db_session.query(EmployeeCourseStatus).filter_by(employee_id=SUBJECT).one()
    assert row.status is CourseStatus.failed
    assert display_status(row.status).value == "not_completed"


# ---------------------------------------------------------------------------
# Provider selection.
# ---------------------------------------------------------------------------

def test_factory_defaults_to_synthetic(db_session, monkeypatch):
    monkeypatch.delenv("ENABLE_TRAINING_API_SYNC", raising=False)
    assert isinstance(get_provider(db_session), SyntheticCertProvider)


def test_factory_returns_training_api_when_enabled(db_session, monkeypatch):
    monkeypatch.setenv("ENABLE_TRAINING_API_SYNC", "true")
    from app.certifications.training_api import TrainingApiProvider

    assert isinstance(get_provider(db_session), TrainingApiProvider)


def test_training_api_is_not_in_the_call_path_when_disabled():
    """Not "instantiated but error-handled" — not imported at all.

    Runs in a fresh interpreter because the assertion is about sys.modules,
    and an earlier test in this file imports the module deliberately.
    """
    script = (
        "import sys\n"
        "from app.db import SessionLocal\n"
        "from app.certifications import get_provider\n"
        "provider = get_provider(SessionLocal())\n"
        "assert provider.name == 'synthetic', provider.name\n"
        "assert 'app.certifications.training_api' not in sys.modules\n"
        "print('ok')\n"
    )
    env = dict(os.environ, ENABLE_TRAINING_API_SYNC="false")
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


def test_provider_reports_not_started_for_a_course_with_no_record(db_session):
    status = SyntheticCertProvider(db_session).get_status(SUBJECT, "T-ALL")
    assert status.status is CourseStatus.not_started
    assert status.source == "synthetic"


# ---------------------------------------------------------------------------
# Profile display.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("role,caller_id,expected_present,case", [
    ("employee", SUBJECT, True, "own profile"),
    ("employee", DIRECT_MANAGER, True, "direct manager"),
    ("employee", SKIP_MANAGER, True, "skip-level manager — full chain, not just the direct one"),
    ("hr", "stranger-1", True, "hr, no relationship"),
    ("employee", "stranger-1", False, "unrelated colleague"),
    ("manager", "stranger-1", False, "manager ROLE without being in the chain"),
])
async def test_training_status_visibility(client, role, caller_id, expected_present, case):
    resp = await client.get(f"/people/{SUBJECT}", headers=auth_headers(role, caller_id))
    assert resp.status_code == 200
    assert ("training_status" in resp.json()) is expected_present, f"{case}: {resp.json()}"


async def test_profile_shows_expected_courses_and_flags_the_rest(client, db_session):
    record_course_status(db_session, caller=_HR_CALLER, employee=_subject(db_session),
                         course_code="T-FIN", status=CourseStatus.completed)

    resp = await client.get(f"/people/{SUBJECT}", headers=auth_headers("employee", SUBJECT))
    items = {i["course_code"]: i for i in resp.json()["training_status"]}

    assert items["T-ALL"]["expected"] is True       # company-wide
    assert items["T-ENG"]["expected"] is True       # their own division
    assert items["T-FIN"]["expected"] is False      # another division's course, taken anyway
    assert items["T-ALL"]["display_status"] == "not_completed"
    assert items["T-ALL"]["display_label"] == "Not completed"
    assert items["T-FIN"]["display_status"] == "completed"


async def test_profile_never_exposes_the_underlying_status(client, db_session):
    """failed and not_started must be indistinguishable in the payload."""
    record_course_status(db_session, caller=_HR_CALLER, employee=_subject(db_session),
                         course_code="T-ALL", status=CourseStatus.failed)

    resp = await client.get(f"/people/{SUBJECT}", headers=auth_headers("hr", "hr-1"))
    item = next(i for i in resp.json()["training_status"] if i["course_code"] == "T-ALL")
    assert "status" not in item
    assert "failed" not in str(item).lower()


async def test_unavailable_provider_omits_the_section_rather_than_guessing(client, monkeypatch):
    """A provider that can't answer must not read as "not completed" — the
    key goes missing, exactly like a field the caller can't see."""
    class Unavailable:
        name = "training_api"

        def __init__(self, db):
            pass

        def get_status(self, employee_id, course_id) -> CertStatus:
            raise CertProviderUnavailable("timeout")

        def list_statuses(self, employee_id) -> list[CertStatus]:
            raise CertProviderUnavailable("timeout")

    monkeypatch.setattr("app.certifications.service.get_provider", lambda db: Unavailable(db))
    resp = await client.get(f"/people/{SUBJECT}", headers=auth_headers("employee", SUBJECT))
    assert resp.status_code == 200
    assert "training_status" not in resp.json()


# ---------------------------------------------------------------------------
# Notification triggers.
# ---------------------------------------------------------------------------

def test_failed_notifies_employee_first_then_the_whole_chain(db_session):
    _row, notes = record_course_status(db_session, caller=_HR_CALLER, employee=_subject(db_session),
                                       course_code="T-ALL", status=CourseStatus.failed)

    assert [n.recipient_id for n in notes] == [SUBJECT, DIRECT_MANAGER, SKIP_MANAGER]
    assert [n.sequence for n in notes] == [0, 1, 2]

    employee_note = notes[0]
    assert employee_note.kind is NotificationKind.employee_course_reminder
    assert "didn't pass" in employee_note.body
    assert "retake" in employee_note.body

    for manager_note in notes[1:]:
        assert manager_note.kind is NotificationKind.manager_course_report
        assert manager_note.body.endswith("did not complete Test Company Wide.")
        # Pass/fail never reaches management, in the body or in the columns.
        assert "pass" not in manager_note.body
        assert "fail" not in manager_note.body
        assert manager_note.display_status == "not_completed"


def test_not_started_tells_the_employee_and_nobody_else(db_session):
    """No attempt, no resolution — the chain has nothing to hear about."""
    _row, notes = record_course_status(db_session, caller=_HR_CALLER, employee=_subject(db_session),
                                       course_code="T-ALL", status=CourseStatus.not_started)

    assert len(notes) == 1
    assert notes[0].recipient_id == SUBJECT
    assert "haven't started" in notes[0].body


def test_completed_notifies_the_chain_but_not_the_employee(db_session):
    _row, notes = record_course_status(db_session, caller=_HR_CALLER, employee=_subject(db_session),
                                       course_code="T-ALL", status=CourseStatus.completed)

    assert [n.recipient_id for n in notes] == [DIRECT_MANAGER, SKIP_MANAGER]
    for note in notes:
        assert note.body.endswith("completed Test Company Wide.")
        assert note.display_status == "completed"


def test_message_wording_differs_by_underlying_status(db_session):
    subject = _subject(db_session)
    _row, first = record_course_status(db_session, caller=_HR_CALLER, employee=subject,
                                       course_code="T-ALL", status=CourseStatus.not_started)
    _row, second = record_course_status(db_session, caller=_HR_CALLER, employee=subject,
                                        course_code="T-ALL", status=CourseStatus.failed)

    assert first[0].display_status == second[0].display_status == "not_completed"
    assert first[0].body != second[0].body


def test_an_unchanged_status_notifies_nobody(db_session):
    subject = _subject(db_session)
    record_course_status(db_session, caller=_HR_CALLER, employee=subject, course_code="T-ALL",
                         status=CourseStatus.in_progress)
    _row, again = record_course_status(db_session, caller=_HR_CALLER, employee=subject, course_code="T-ALL",
                                       status=CourseStatus.in_progress)
    assert again == []


def test_chain_depth_is_configurable(db_session, monkeypatch):
    """Full chain is what we want today, but it's a setting — turning it down
    must not need an edit to notification logic."""
    monkeypatch.setenv("NOTIFY_LEVELS_UP", "1")
    _row, notes = record_course_status(db_session, caller=_HR_CALLER, employee=_subject(db_session),
                                       course_code="T-ALL", status=CourseStatus.completed)
    assert [n.recipient_id for n in notes] == [DIRECT_MANAGER]


def test_chain_depth_zero_leaves_the_employee_trigger_running(db_session, monkeypatch):
    monkeypatch.setenv("NOTIFY_LEVELS_UP", "0")
    _row, notes = record_course_status(db_session, caller=_HR_CALLER, employee=_subject(db_session),
                                       course_code="T-ALL", status=CourseStatus.failed)
    assert [n.recipient_id for n in notes] == [SUBJECT]


def test_service_denies_a_non_hr_caller_even_without_a_route(db_session):
    """The route already blocks this, but the service must too — a future
    caller that skips the route (a batch import, a webhook handler) gets the
    same refusal, not an unauthenticated write."""
    non_hr = AuthenticatedUser(id="manager-caller-test", role="manager")
    with pytest.raises(RecordCourseStatusDenied):
        record_course_status(db_session, caller=non_hr, employee=_subject(db_session),
                             course_code="T-ALL", status=CourseStatus.failed)
    assert db_session.query(EmployeeCourseStatus).filter_by(employee_id=SUBJECT).one_or_none() is None


# ---------------------------------------------------------------------------
# Routes.
# ---------------------------------------------------------------------------

async def test_recording_a_status_is_hr_only(client):
    resp = await client.post(f"/people/{SUBJECT}/training/T-ALL", json={"status": "failed"},
                             headers=auth_headers("manager", DIRECT_MANAGER))
    assert resp.status_code == 403


async def test_record_route_fires_the_triggers(client):
    resp = await client.post(f"/people/{SUBJECT}/training/T-ALL", json={"status": "failed"},
                             headers=auth_headers("hr", "hr-1"))
    assert resp.status_code == 201
    body = resp.json()
    assert body["display_status"] == "not_completed"
    assert body["notifications_sent"] == 3  # the employee + two levels of chain


async def test_notifications_route_returns_only_your_own(client):
    await client.post(f"/people/{SUBJECT}/training/T-ALL", json={"status": "failed"},
                      headers=auth_headers("hr", "hr-1"))

    own = await client.get("/me/notifications", headers=auth_headers("employee", SUBJECT))
    assert own.status_code == 200
    assert [n["kind"] for n in own.json()] == ["employee_course_reminder"]
    assert own.json()[0]["subject_person"]["id"] == SUBJECT

    manager = await client.get("/me/notifications", headers=auth_headers("manager", DIRECT_MANAGER))
    assert [n["kind"] for n in manager.json()] == ["manager_course_report"]

    uninvolved = await client.get("/me/notifications", headers=auth_headers("hr", "hr-1"))
    assert uninvolved.json() == []


async def test_unknown_course_is_a_404(client):
    resp = await client.post(f"/people/{SUBJECT}/training/NOPE-999", json={"status": "failed"},
                             headers=auth_headers("hr", "hr-1"))
    assert resp.status_code == 404
