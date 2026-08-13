"""What the profile actually renders: expectations joined to reported status.

This is the only place the two halves meet. Everything above it (routes,
people.py) sees a list of TrainingStatusItem and has no idea a provider
exists; everything below it (providers, requirements) knows one half each.

Note what is NOT in the output shape: the four-value status. The profile
shows the two-value derivation, per spec — the underlying not_started /
in_progress / failed distinction stays server-side, where the notification
copy needs it.
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config
from app.certifications.base import CertProviderUnavailable, CertStatus, UnknownCourse
from app.certifications.factory import get_provider
from app.certifications.requirements import required_courses
from app.models import Employee, EmployeeCourseStatus, Notification, TrainingCourse
from app.models.enums import DISPLAY_STATUS_LABEL, CourseStatus
from app.schemas import TrainingStatusItem


def _month(d: date | None) -> str | None:
    """Month/year granularity, same as project history and away_until — an
    exact date says more about someone's week than the directory needs to."""
    return d.strftime("%Y-%m") if d else None


def _to_item(status: CertStatus, *, expected: bool) -> TrainingStatusItem:
    return TrainingStatusItem(
        course_code=status.course_id,
        course_name=status.course_name,
        display_status=status.display_status.value,
        display_label=DISPLAY_STATUS_LABEL[status.display_status],
        expected=expected,
        attempted_month=_month(status.attempted_at),
        completed_month=_month(status.completed_at),
        source=status.source,
    )


def employee_training_status(db: Session, employee: Employee) -> list[TrainingStatusItem] | None:
    """Training status for one employee, or None if the provider couldn't
    answer.

    None and [] mean different things and the caller must keep them apart:
    [] is "no courses apply to this person", a real answer; None is "we
    don't know right now", and the profile drops the section rather than
    render an authoritative-looking "not completed" that no one verified.
    Same degradation stance as search_client returning None when Azure
    Search is unreachable — degrade, never assert something false.
    """
    provider = get_provider(db)

    try:
        reported = {s.course_id: s for s in provider.list_statuses(employee.id)}
        expected_courses = required_courses(db, employee)

        items: list[TrainingStatusItem] = []
        seen: set[str] = set()
        for course in expected_courses:
            status = reported.get(course.code)
            if status is None:
                # Expected, but the provider has no record. Asking the
                # provider rather than assuming keeps the "no record means
                # not_started" semantics in one place — and keeps an
                # unavailable provider raising instead of quietly becoming a
                # not-started reminder. list_statuses() above already covers
                # everyone who has any history, so this is a short tail.
                status = provider.get_status(employee.id, course.code)
            items.append(_to_item(status, expected=True))
            seen.add(course.code)

        # Courses they have history against that nobody requires of them —
        # shown, but flagged, so "did extra training" never reads as
        # "outstanding requirement".
        for code, status in sorted(reported.items(), key=lambda kv: kv[1].course_name):
            if code not in seen:
                items.append(_to_item(status, expected=False))

        return items
    except CertProviderUnavailable:
        return None


class LocalStatusWritesDisabled(RuntimeError):
    """record_course_status() was called while the real training API is the
    source of truth. Writing our own row then would create a second, silently
    diverging truth."""


def record_course_status(
    db: Session,
    *,
    employee: Employee,
    course_code: str,
    status: CourseStatus,
    attempted_on: date | None = None,
    completed_on: date | None = None,
) -> tuple[EmployeeCourseStatus, list[Notification]]:
    """Record a status change and fire the notification triggers.

    This is the seam the training system's own updates will arrive through.
    Today it's driven by an endpoint (POST /people/{id}/training/{course},
    hr-only) that stands in for the training team pushing us an event, which
    is what makes the notification behaviour demoable on synthetic data.
    When the webhook-vs-poll question is settled, the handler for it calls
    this same function with the same (previous, current) transition and
    nothing downstream changes.

    Refuses to run once ENABLE_TRAINING_API_SYNC is on: at that point their
    system owns the status and our table is a cache, so accepting a local
    write would mean two sources of truth disagreeing quietly.
    """
    # Imported here rather than at module scope: app.notifications reaches
    # app.org_chart -> app.people, and app.people imports this package for
    # the profile's read path, so a top-level import would close that cycle.
    # Keeping it local also means importing the read path never drags the
    # write path in behind it.
    from app.notifications import on_course_status_changed

    if config.enable_training_api_sync():
        raise LocalStatusWritesDisabled(
            "ENABLE_TRAINING_API_SYNC is on — course status comes from the training API, "
            "not from local writes"
        )

    course = db.execute(
        select(TrainingCourse).where(TrainingCourse.code == course_code)
    ).scalar_one_or_none()
    if course is None:
        raise UnknownCourse(f"No training course with code {course_code!r}")

    row = db.execute(
        select(EmployeeCourseStatus).where(
            EmployeeCourseStatus.employee_id == employee.id,
            EmployeeCourseStatus.course_id == course.id,
        )
    ).scalar_one_or_none()

    # None, not not_started: "we have never had a status for this pair" is a
    # different thing from "we know they haven't started", and the trigger
    # treats them differently.
    previous = row.status if row is not None else None

    today = date.today()
    attempted = attempted_on or (row.attempted_at if row else None)
    if attempted is None and status is not CourseStatus.not_started:
        attempted = today
    # Cleared on anything but a pass — someone who retakes and fails is not
    # still holding a completion date.
    completed = (completed_on or today) if status is CourseStatus.completed else None

    if row is None:
        row = EmployeeCourseStatus(
            employee_id=employee.id, course_id=course.id, status=status,
            attempted_at=attempted, completed_at=completed,
            source="synthetic", last_synced_at=datetime.now(),
        )
        db.add(row)
    else:
        row.status = status
        row.attempted_at = attempted
        row.completed_at = completed
        row.last_synced_at = datetime.now()
    db.flush()

    notifications = on_course_status_changed(
        db, employee=employee, course=course, previous=previous, current=status,
    )
    db.commit()
    db.refresh(row)
    return row, notifications
