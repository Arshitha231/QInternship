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
from dataclasses import dataclass
from datetime import date, datetime

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
    is_milestone_year,
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


def _may_receive(db: Session, recipient: Employee, subject: Employee,
                 requires_field: str | None) -> bool:
    """May this recipient be told something about this subject?

    `requires_field` names the profile field the notification's content comes
    from, so entitlement to the notification is decided by exactly the rule
    that governs seeing it on the profile — one source of truth, not two.
    None means the notification reveals nothing beyond the person's existence
    (a birthday reminder names them and nothing else, in particular never
    their date of birth or age), so seeing the record at all is the bar.
    """
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
    if requires_field is None:
        return True
    return requires_field in visible_fields(db, caller, subject)


def _send(
    db: Session,
    *,
    recipient: Employee,
    subject: Employee,
    kind: NotificationKind,
    body: str,
    course: TrainingCourse | None = None,
    display: CourseDisplayStatus | None = None,
    sequence: int = 0,
    levels_up: int = 0,
    event_key: str | None = None,
    requires_field: str | None = None,
) -> Notification | None:
    """Create one notification, or drop it if the recipient isn't entitled to
    it. Dropped silently on purpose — redact, never reject, same as the rest
    of the API; the audit entry written by the caller still counts what
    actually went out."""
    if not recipient.is_active or not _may_receive(db, recipient, subject, requires_field):
        return None

    notification = Notification(
        recipient_id=recipient.id, subject_employee_id=subject.id,
        course_id=course.id if course is not None else None,
        kind=kind, display_status=display.value if display is not None else "",
        body=body, sequence=sequence, levels_up=levels_up,
        event_key=event_key, created_at=datetime.now(),
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
            requires_field=TRAINING_FIELD,
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
                requires_field=TRAINING_FIELD,
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
# Manual reminders: HR or a manager nudging named people about a named course
#
# The third way a course notification can come to exist, and the only one a
# human initiates. The other two fire from a status change; this one fires
# because somebody looked at a compliance dashboard and pressed a button.
#
# What it deliberately reuses rather than reinvents: _send (so the same
# _may_receive permission check and the same _deliver seam apply), the same
# employee_course_reminder kind (an employee's inbox should not sprout a
# second species of "you haven't finished X" that renders differently), and
# event_key (so a double-click, a retried request, or two HR people working
# the same list on the same day produce one reminder, not three).
#
# What it adds: the deadline. A status-change reminder can't mention one —
# it fires the instant a status changes and the due date isn't part of that
# event — whereas the whole reason someone is pressing the button is that a
# date is approaching or has passed, so saying which date is the point.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ReminderTarget:
    """One (person, course) pair to nudge, with the facts the wording needs.

    Assembled by the caller (app/analytics.py, off the dashboard's own
    buckets) rather than re-derived here: the caller has already resolved
    who is expected to do what by when in order to render the list the user
    selected from, and recomputing it would risk sending a reminder that
    disagrees with the screen it was sent from.
    """

    employee: Employee
    course: TrainingCourse
    status: CourseStatus
    due_on: date | None = None


def reminder_message(status: CourseStatus, course_name: str, due_on: date | None,
                     on_date: date) -> str:
    """The status-specific wording, plus a deadline clause when there is a
    deadline to state.

    Built on employee_message rather than beside it: "you didn't pass" vs
    "you haven't started" is the same distinction here as on the automatic
    trigger, and having two places decide it is how the two drift apart. The
    deadline clause is appended, never substituted, so the actionable part
    survives regardless of whether a due date exists.

    A course with no recorded deadline gets no clause at all — not "due
    soon", not an invented date. Nothing in the training data says when it
    is due, so neither does the message.
    """
    base = employee_message(status, course_name)
    if due_on is None:
        return base
    if due_on < on_date:
        days = (on_date - due_on).days
        return f"{base} It was due on {due_on.isoformat()} — {days} day{'s' if days != 1 else ''} ago."
    if due_on == on_date:
        return f"{base} It's due today."
    days = (due_on - on_date).days
    return f"{base} It's due on {due_on.isoformat()}, in {days} day{'s' if days != 1 else ''}."


def send_course_reminders(
    db: Session,
    *,
    actor_id: str,
    targets: list[ReminderTarget],
    on_date: date | None = None,
) -> list[Notification]:
    """Send one reminder per target, skipping any already sent today.

    Returns what was actually created — which is not necessarily one per
    target, and the caller is expected to say so rather than report the
    input count back as a success figure. Three things drop a target
    silently, all of them deliberate: an inactive employee, a recipient
    _send judges not entitled (redact, never reject, same as everywhere
    else), and a same-day duplicate.

    Reminders about a course somebody has already completed are refused
    here, not filtered by the caller. It is the one mistake this endpoint
    could make that lands in a real person's inbox and is visibly wrong to
    them, so the guard belongs at the last point before the row is written.

    Caller commits, matching on_course_status_changed.
    """
    on_date = on_date or date.today()
    created: list[Notification] = []
    if not targets:
        _write_reminder_audit(db, actor_id, on_date, 0, 0)
        return created

    keys = {
        f"course-reminder:{on_date.isoformat()}:{t.employee.id}:{t.course.code}"
        for t in targets
    }
    # One query for every key already used today, not an existence check per
    # target — same reasoning as the date-milestone sweep above.
    already_sent: set[str] = {
        row.event_key
        for row in db.execute(
            select(Notification.event_key).where(Notification.event_key.in_(keys))
        ).all()
        if row.event_key is not None
    }

    for target in targets:
        if display_status(target.status) is CourseDisplayStatus.completed:
            continue
        event_key = (f"course-reminder:{on_date.isoformat()}:"
                     f"{target.employee.id}:{target.course.code}")
        if event_key in already_sent:
            continue
        sent = _send(
            db, recipient=target.employee, subject=target.employee, course=target.course,
            kind=NotificationKind.employee_course_reminder,
            body=reminder_message(target.status, target.course.name, target.due_on, on_date),
            display=CourseDisplayStatus.not_completed,
            sequence=0, levels_up=0, event_key=event_key,
            requires_field=TRAINING_FIELD,
        )
        if sent is not None:
            created.append(sent)
            already_sent.add(event_key)

    _write_reminder_audit(db, actor_id, on_date, len(targets), len(created))
    return created


def _write_reminder_audit(db: Session, actor_id: str, on_date: date,
                          requested: int, sent: int) -> None:
    """Actor is the person who pressed the button, unlike the course and date
    triggers whose actor is the subject or "system" — a manual reminder has a
    human behind it and the audit row should name them."""
    db.add(AuditLog(
        actor_id=actor_id, action="send_course_reminders",
        query_text=f"on_date={on_date.isoformat()};requested={requested}",
        result_count=sent,
        fields_returned=json.dumps(["recipient_id", "kind", "display_status", "body", "event_key"]),
        timestamp=datetime.now(),
    ))


# ---------------------------------------------------------------------------
# Date-driven triggers: birthdays and milestone work anniversaries, to HR
#
# Structurally different from the course triggers above. Those fire from a
# state change — something happened, so something is sent. Nothing changes in
# the database on someone's birthday, so these have to be a sweep: a caller
# asks "what falls on this date", every day, and the answer is computed rather
# than observed.
#
# That has two consequences the course path doesn't have. It needs an external
# caller (there is no scheduler in this project — see the route in main.py,
# which is what a daily cron or Azure timer would hit), and it must be safe to
# run twice, because anything that runs daily eventually runs twice. Hence
# event_key.
# ---------------------------------------------------------------------------

def _falls_on(anniversary: date, on_date: date) -> bool:
    """Does `anniversary`'s month/day land on `on_date`?

    29 February is the whole reason this isn't a two-field comparison. In a
    non-leap year those birthdays would otherwise never come round at all, so
    they're observed on the 28th — the same convention most payroll and HR
    systems use, and better than silently skipping someone every three years
    out of four.
    """
    if anniversary.month == on_date.month and anniversary.day == on_date.day:
        return True
    is_leap_day = anniversary.month == 2 and anniversary.day == 29
    on_feb_28 = on_date.month == 2 and on_date.day == 28
    if is_leap_day and on_feb_28:
        # Only when this February has no 29th of its own to fall on.
        return not _is_leap(on_date.year)
    return False


def _is_leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def _years_completed(start: date, on_date: date) -> int:
    """Whole years from `start` to `on_date`."""
    years = on_date.year - start.year
    if (on_date.month, on_date.day) < (start.month, start.day):
        years -= 1
    return years


def hr_recipients(db: Session) -> list[Employee]:
    """Active employees in the HR org unit or anywhere beneath it.

    Resolved from the org tree because there is no role column to read and a
    sweep has no request to carry a role claim on — see config.hr_org_unit_name
    for the full reasoning. Returns [] if the configured unit doesn't exist,
    which means the sweep quietly notifies nobody rather than crashing; the
    audit entry still records that it ran and found zero recipients.
    """
    # Reuses find_people's subtree resolver rather than duplicating the
    # recursive CTE — "everyone under this unit" needs to mean exactly one
    # thing in this codebase, and that's where it's already defined.
    from app.people import _org_unit_and_descendant_ids

    unit_ids = _org_unit_and_descendant_ids(db, config.hr_org_unit_name())
    if not unit_ids:
        return []
    return list(db.execute(
        select(Employee)
        .where(Employee.org_unit_id.in_(unit_ids), Employee.is_active == True)
        .order_by(Employee.full_name)
    ).scalars().all())


def birthday_message(subject: Employee) -> str:
    """Names the person and nothing else. Deliberately carries no date of
    birth and no age: HR is being told to mark the occasion, not handed a
    field that lives behind a stricter permission than this notification.

    Full name, not preferred name — these go to HR about someone they may not
    know, and "It's Matt's birthday today" doesn't identify anyone in a
    530-person company. Same reasoning as the manager-facing course reports.
    """
    return f"It's {subject.full_name}'s birthday today."


def anniversary_message(subject: Employee, years: int) -> str:
    return (f"{subject.full_name} reaches "
            f"{years} year{'s' if years != 1 else ''} at Quadrant today.")


def notify_date_milestones(db: Session, on_date: date | None = None) -> list[Notification]:
    """Birthdays and milestone service anniversaries falling on `on_date`.

    Idempotent: re-running for the same date sends nothing further, so a
    retried cron or a restarted container can't produce a second birthday
    message. Caller commits.
    """
    on_date = on_date or date.today()
    created: list[Notification] = []

    recipients = hr_recipients(db)
    if not recipients:
        _write_date_audit(db, on_date, 0, 0)
        return created

    # One query for every key already used on this date, rather than an
    # existence check per (recipient, subject) pair — a 500-person company
    # with a dozen HR staff would otherwise be thousands of round trips for a
    # sweep that usually creates nothing.
    already_sent: set[tuple[str, str]] = {
        (row.recipient_id, row.event_key)
        for row in db.execute(
            select(Notification.recipient_id, Notification.event_key)
            .where(Notification.event_key.is_not(None),
                   Notification.event_key.like(f"%:{on_date.isoformat()}:%"))
        ).all()
    }

    people = db.execute(
        select(Employee).where(Employee.is_active == True).order_by(Employee.full_name)
    ).scalars().all()

    birthdays = 0
    anniversaries = 0
    for subject in people:
        events: list[tuple[NotificationKind, str, str]] = []

        if subject.date_of_birth is not None and _falls_on(subject.date_of_birth, on_date):
            events.append((
                NotificationKind.birthday_reminder,
                f"birthday:{on_date.isoformat()}:{subject.id}",
                birthday_message(subject),
            ))

        if _falls_on(subject.hire_date, on_date):
            years = _years_completed(subject.hire_date, on_date)
            if is_milestone_year(years):
                events.append((
                    NotificationKind.work_anniversary_reminder,
                    f"anniversary:{on_date.isoformat()}:{subject.id}",
                    anniversary_message(subject, years),
                ))

        for kind, event_key, body in events:
            sent_any = False
            for recipient in recipients:
                # An HR person doesn't need telling it's their own birthday.
                # They still appear in everyone else's sweep, so the occasion
                # isn't missed — it just isn't self-addressed.
                if recipient.id == subject.id:
                    continue
                if (recipient.id, event_key) in already_sent:
                    continue
                sent = _send(
                    db, recipient=recipient, subject=subject, kind=kind,
                    body=body, event_key=event_key,
                    # No requires_field: the body names the person and reveals
                    # nothing that sits behind a field permission. Notably NOT
                    # keyed on date_of_birth, which is self-and-HR-only —
                    # gating on it would be a coincidence of today's roles
                    # rather than a statement about this message.
                    requires_field=None,
                )
                if sent is not None:
                    created.append(sent)
                    sent_any = True
            if sent_any:
                if kind is NotificationKind.birthday_reminder:
                    birthdays += 1
                else:
                    anniversaries += 1

    _write_date_audit(db, on_date, birthdays, anniversaries)
    return created


def _write_date_audit(db: Session, on_date: date, birthdays: int, anniversaries: int) -> None:
    db.add(AuditLog(
        actor_id="system", action="notify_date_milestones",
        query_text=f"on_date={on_date.isoformat()}",
        result_count=birthdays + anniversaries,
        fields_returned=json.dumps(["recipient_id", "kind", "body", "event_key"]),
        timestamp=datetime.now(),
    ))


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
