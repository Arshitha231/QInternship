"""Two independent triggers off one status-change event.

    employee reminder    fires whenever display_status is not_completed.
                         Wording depends on the UNDERLYING status — that's
                         the whole reason the four-value enum survives into
                         the database instead of being collapsed to two.
    management report    fires on a status *resolution* (completed, or
                         not_completed after an actual attempt), and walks
                         the full reporting chain, not just the direct
                         manager. Says "completed" or "did not complete" and
                         never more: pass/fail is between the employee and
                         the training system.

ORDERING (explicit, not incidental)
-----------------------------------
The employee's own notification is created first and carries sequence 0; the
chain follows at 1..n, ordered by distance up the tree. Both are written in
one transaction and committed together, so the guarantee is "the employee is
told before, or at the same instant as, their management" — never after.

This matters most in the failed case: finding out you didn't pass by way of
your skip-level manager asking about it is a bad day at work. Leaving the
order to whichever subscriber a dispatcher happened to call first would make
that a coin flip, so there is no dispatcher and no subscribers — one
function, in one order, with the order persisted on the row (`sequence`)
rather than inferred from created_at, which is only millisecond-resolution
and would leave ties unresolved.

PERMISSIONS
-----------
Every notification goes through _may_receive(), which asks the same
app.permissions functions the profile API asks. Nothing here calls a mailer
or a Slack client directly — _deliver() is the single seam a real transport
plugs into, and it is downstream of the permission check, not around it. A
recipient who could not see the field on the profile does not get told about
it in a notification either.
"""
from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import config
from app.auth import AuthenticatedUser, Role
from app.models import AuditLog, Employee, Notification, TrainingCourse
from app.models.enums import (
    CourseDisplayStatus,
    CourseStatus,
    NotificationKind,
    display_status,
)
from app.org_chart import manager_chain_ids
from app.permissions import is_record_visible, visible_fields

# The field these notifications are about. A recipient who can't see it on
# the profile can't be told about it either — one rule, checked in one place.
TRAINING_FIELD = "training_status"

# A status is "resolved" once there's an outcome to report upward. An
# untouched course isn't news for management, and a course someone is
# midway through is not an outcome yet — the employee still hears about
# both, they're just not the chain's business.
RESOLVED_STATUSES = frozenset({CourseStatus.completed, CourseStatus.failed})


# ---------------------------------------------------------------------------
# Message text
# ---------------------------------------------------------------------------

def employee_message(status: CourseStatus, course_name: str) -> str:
    """Wording by underlying status. All three of these render under the same
    "Not completed" label on the profile — the label is the summary, this is
    the part that has to be actionable."""
    if status is CourseStatus.not_started:
        return f"You haven't started {course_name} yet."
    if status is CourseStatus.failed:
        return f"You didn't pass {course_name} — you'll need to retake it."
    if status is CourseStatus.in_progress:
        # Not one of the two variants originally specified; in_progress maps
        # to not_completed like the others, so the trigger fires and needs
        # copy. Flagged in the README as an inferred case to confirm.
        return f"You've started {course_name} but haven't finished it yet."
    return f"You have completed {course_name}."


def manager_message(employee_name: str, status: CourseStatus, course_name: str) -> str:
    """Completed / did not complete, and nothing finer. A manager reading
    "did not complete" cannot tell a failed attempt from an unstarted
    course, by design — pass/fail is not management-facing data."""
    if display_status(status) is CourseDisplayStatus.completed:
        return f"{employee_name} completed {course_name}."
    return f"{employee_name} did not complete {course_name}."


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------

def _deliver(notification: Notification) -> None:
    """The single seam where a real transport plugs in.

    There is no email or chat integration in this project, so persisting the
    row IS the delivery — the recipient reads it from GET /me/notifications.
    A real transport (Graph sendMail, Slack, Teams) goes here and nowhere
    else, so it inherits the permission check above it for free and can't be
    reached around.
    """
    return None


# ---------------------------------------------------------------------------
# Permission-checked send path
# ---------------------------------------------------------------------------

def _role_for(db: Session, employee: Employee) -> Role:
    """The recipient's role, for the permission check.

    There is no role column: roles arrive on the request, from a dev header
    or an Entra app-role claim (app/auth.py), and a notification has no
    request. Having direct reports is the one role signal derivable from the
    data we own, and it's the conservative one — it never invents "hr", the
    role that unlocks anything, and the two roles it does pick between grant
    identical base fields. When Entra app roles are live, this should read
    the same claim source auth.py maps rather than guessing from the graph.
    """
    has_reports = db.execute(
        select(Employee.id).where(Employee.manager_id == employee.id, Employee.is_active == True).limit(1)
    ).first()
    return "manager" if has_reports else "employee"


def _may_receive(db: Session, recipient: Employee, subject: Employee) -> bool:
    """Would this recipient be allowed to see the subject's training status
    on their profile? Then they may be told about it. Otherwise not."""
    if recipient.id == subject.id:
        # You are always allowed to be told about yourself. Worth stating
        # because is_record_visible() would say otherwise for the handful of
        # 'restricted' employees, whose own records are hidden from every
        # non-hr caller including themselves — a directory-listing rule that
        # has no business suppressing someone's own training reminder.
        return True
    caller = AuthenticatedUser(id=recipient.id, role=_role_for(db, recipient),
                               name=recipient.full_name, email=recipient.work_email)
    if not is_record_visible(caller, subject):
        return False
    return TRAINING_FIELD in visible_fields(db, caller, subject)


def _send(
    db: Session,
    *,
    recipient: Employee,
    subject: Employee,
    course: TrainingCourse,
    kind: NotificationKind,
    body: str,
    display: CourseDisplayStatus,
    sequence: int,
    levels_up: int,
) -> Notification | None:
    """Create one notification, or drop it if the recipient isn't entitled to
    it. Dropped silently on purpose — redact, never reject, same as the rest
    of the API; the audit entry written by the caller still counts what
    actually went out."""
    if not recipient.is_active or not _may_receive(db, recipient, subject):
        return None

    notification = Notification(
        recipient_id=recipient.id, subject_employee_id=subject.id, course_id=course.id,
        kind=kind, display_status=display.value, body=body,
        sequence=sequence, levels_up=levels_up, created_at=datetime.now(),
    )
    db.add(notification)
    _deliver(notification)
    return notification


def _write_audit(db: Session, subject: Employee, query_text: str, count: int) -> None:
    db.add(AuditLog(
        actor_id=subject.id, action="notify_course_status", query_text=query_text,
        result_count=count,
        fields_returned=json.dumps(["recipient_id", "kind", "display_status", "body"]),
        timestamp=datetime.now(),
    ))


# ---------------------------------------------------------------------------
# The trigger
# ---------------------------------------------------------------------------

def on_course_status_changed(
    db: Session,
    *,
    employee: Employee,
    course: TrainingCourse,
    previous: CourseStatus | None,
    current: CourseStatus,
) -> list[Notification]:
    """Fire both triggers for one status change. Returns what was actually
    created, in send order.

    `previous is None` means "first time we've seen a status for this pair" —
    treated as a change, since a newly-imposed requirement someone hasn't
    started is exactly the reminder case. An unchanged status is not a change
    and fires nothing: a re-sync that reports the same value must not
    re-notify anybody.

    Caller commits. Both notifications are created inside the caller's
    transaction so they land together — see the ordering note at the top of
    this module.
    """
    created: list[Notification] = []
    if previous == current:
        return created

    display = display_status(current)

    # --- 1. the employee, first, always -----------------------------------
    if display is CourseDisplayStatus.not_completed:
        sent = _send(
            db, recipient=employee, subject=employee, course=course,
            kind=NotificationKind.employee_course_reminder,
            body=employee_message(current, course.name),
            display=display, sequence=0, levels_up=0,
        )
        if sent is not None:
            created.append(sent)

    # --- 2. the reporting chain, after ------------------------------------
    if current in RESOLVED_STATUSES:
        # Depth is config, not policy baked into this function: -1 (the
        # default, and what's wanted today) is the full chain, 1 would be
        # direct manager only, 0 turns the management-facing trigger off
        # entirely — all without touching a line of this file.
        chain = manager_chain_ids(db, employee.id, config.notify_levels_up())
        for index, manager_id in enumerate(chain, start=1):
            manager = db.get(Employee, manager_id)
            if manager is None:
                continue
            sent = _send(
                db, recipient=manager, subject=employee, course=course,
                kind=NotificationKind.manager_course_report,
                body=manager_message(employee.full_name, current, course.name),
                display=display, sequence=index, levels_up=index,
            )
            if sent is not None:
                created.append(sent)

    _write_audit(
        db, employee,
        f"employee_id={employee.id};course={course.code};"
        f"previous={previous.value if previous else None};current={current.value}",
        len(created),
    )
    return created


# ---------------------------------------------------------------------------
# Reading them back
# ---------------------------------------------------------------------------

def notifications_for(db: Session, recipient_id: str, limit: int = 50) -> list[Notification]:
    """A recipient's own notifications, newest first. The route enforces that
    the caller IS the recipient — there is no "read someone else's inbox"
    path, for any role."""
    return list(db.execute(
        select(Notification)
        .where(Notification.recipient_id == recipient_id)
        .order_by(Notification.created_at.desc(), Notification.sequence)
        .limit(limit)
    ).scalars().all())
