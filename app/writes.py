"""Write paths for hr (internal employee fields) and it (project
descriptions).

Enforcement lives HERE, not in the route. The route is one caller; the
tool-calling layer, a future batch import, and any test that calls the
service directly are others, and a rule that only exists in a FastAPI
decorator is a rule that only applies to callers who happen to come through
FastAPI. Every function below re-derives (role, view_mode) permission from
app.permissions' EDITABLE table before touching a row — the read that the UI
performed first is not evidence of anything.

Each write follows the same four steps, in this order:

    1. authorize (role + view_mode + field, from the table)
    2. persist
    3. re-index (rule 6)
    4. audit

Audit last and unconditionally: a write that succeeded and then failed to
re-index is still a write that happened, and the audit row is what says so.

One READ lives here too — list_deactivated_employees. It sits with the
lifecycle functions rather than in app/people.py because every read path in
that module treats is_active=False as nonexistent (deliberately), and it's
gated by the same EDITABLE capability as deactivate/reactivate rather than
by directory visibility. Putting it next to find_people/get_person would
place a function whose whole job is to surface inactive records beside
functions whose whole contract is that they don't.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import case, select
from sqlalchemy.orm import Session

from app.auth import AuthenticatedUser
from app.models import (
    AuditLog, CommunityLink, Employee, EmployeeActionRequest, EmployeeProject, Notification, Office,
    OrgUnit, Project,
)
from app.models.enums import (
    AvailabilityStatus, CommunityLinkSource, EmployeeActionStatus, EmployeeActionType, EmploymentType,
    NotificationKind,
)
from app.org_chart import manager_chain_ids
from app.permissions import ViewMode, can_edit, editable_fields
from app.project_search import reindex_project
from app.search_reindex import reindex_employee, reindex_employee_id


class WriteDenied(Exception):
    """Role/view_mode/field combination is not permitted.

    Distinct from "not found" on purpose. The redact-never-reject rule
    governs *reads* — a caller who may not see a record is told nothing
    exists. A write is different: the caller is asserting an intent to
    change data, and silently accepting it while doing nothing would be a
    worse answer than a plain refusal. Reads still 404; writes 403.
    """


class WriteTargetMissing(Exception):
    """No such employee/project/membership."""


class EmployeeAlreadyInactive(Exception):
    """deactivate_employee on a target that's already is_active=False."""


class HasActiveDirectReports(Exception):
    """deactivate_employee refused: reassign these people first.

    Carries the list rather than just a count — the route turns this
    straight into the response body, so the caller (the frontend's inline
    reassignment picker) never needs a second round trip just to find out
    who's blocking it.
    """

    def __init__(self, reports: list[dict]):
        self.reports = reports
        super().__init__(f"{len(reports)} active direct report(s) must be reassigned first")


class DuplicateEmail(Exception):
    """create_employee: work_email already belongs to another employee."""


class NoApproverAvailable(Exception):
    """request_restriction/request_deactivation: the requester's entire
    reporting chain is exhausted (app.writes._resolve_approver) with nobody
    reachable to approve. Refused outright rather than staged with a null
    approver that could never be acted on."""


class RequestNotPending(Exception):
    """approve_action_request/reject_action_request: already resolved."""


# Fields whose values need coercion out of JSON into the column's type.
_DATE_FIELDS = {"date_of_birth", "hire_date"}
_DECIMAL_FIELDS = {"salary"}


def _coerce(field: str, value):
    if value is None:
        return None
    if field in _DATE_FIELDS:
        if isinstance(value, date):
            return value
        try:
            return date.fromisoformat(str(value))
        except ValueError as exc:
            raise ValueError(f"{field} must be an ISO date (YYYY-MM-DD)") from exc
    if field in _DECIMAL_FIELDS:
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"{field} must be a number") from exc
    if field == "employment_type":
        # The column is Enum(..., validate_strings=True), which would accept
        # the raw string — converting here anyway so the in-memory object
        # holds the same enum type it would after a refresh, rather than a
        # str that compares unequal to EmploymentType.fte until reloaded.
        return value if isinstance(value, EmploymentType) else EmploymentType(value)
    if field == "availability_status":
        return value if isinstance(value, AvailabilityStatus) else AvailabilityStatus(value)
    return value


def _audit(
    db: Session, caller: AuthenticatedUser, action: str, query_text: str,
    fields: set[str] | list[str], result_count: int = 1, source: str | None = None,
) -> None:
    db.add(AuditLog(
        actor_id=caller.id, action=action, query_text=query_text,
        result_count=result_count, fields_returned=json.dumps(sorted(fields)),
        source=source, timestamp=datetime.now(),
    ))
    db.commit()


def _authorize(role: str, view_mode: ViewMode, fields) -> None:
    denied = sorted(f for f in fields if not can_edit(role, view_mode, f))
    if denied:
        raise WriteDenied(
            f"role '{role}' in {view_mode} mode may not edit: {', '.join(denied)}. "
            f"Editable here: {', '.join(sorted(editable_fields(role, view_mode))) or 'nothing'}"
        )


# ---------------------------------------------------------------------------
# HR, work mode: edit internal fields on any employee.
# ---------------------------------------------------------------------------

def update_employee(
    db: Session, caller: AuthenticatedUser, person_id: str, changes: dict,
    view_mode: ViewMode,
) -> Employee:
    """PATCH semantics: only the keys present are touched.

    An explicit null clears the field — which is why this takes the raw dict
    of supplied keys rather than a fully-populated model. "salary": null
    means "no salary on file", and must stay distinguishable from omitting
    salary entirely, the same distinction the read side maintains between an
    absent key and a null one.
    """
    if not changes:
        raise ValueError("no fields supplied")

    _authorize(caller.role, view_mode, changes.keys())

    # No self-service through the admin edit path, for anyone who reaches
    # it — today that's hr alone, since EDITABLE grants update_employee
    # fields to no other (role, view_mode) pair, but the check is written
    # against the endpoint's own rule ("edit anyone's record") rather than
    # hardcoded to the role, so it stays correct if EDITABLE ever grows a
    # second entry here. The obvious hole this closes: an hr caller giving
    # themselves a raise, or clearing their own cost_centre, through the
    # same endpoint that edits everyone else's.
    if person_id == caller.id:
        raise WriteDenied(
            f"role '{caller.role}' may edit any employee's record except their own "
            f"(person_id == caller.id)"
        )

    target = db.get(Employee, person_id)
    if target is None or not target.is_active:
        raise WriteTargetMissing(person_id)

    # Restricting is the one availability_status value this generic path
    # refuses — it's a maker-checker action now (see request_restriction),
    # not a single-actor field edit. "available"/"away" stay ordinary
    # PATCHable values; only the transition INTO "restricted" is gated.
    if changes.get("availability_status") == "restricted":
        raise ValueError(
            "restricting a profile requires approval — use POST /employees/{id}/restrict instead"
        )

    # manager_id isn't type-coerced like a date or a decimal — it's a
    # reference, and the only thing that can make it wrong is pointing
    # somewhere nonsensical. Checked here, once, rather than trusted:
    # nothing else in this codebase writes manager_id today, so this is the
    # one place that gets to decide what a valid manager reference is.
    if "manager_id" in changes:
        new_manager_id = changes["manager_id"]
        if new_manager_id is not None:
            if new_manager_id == person_id:
                raise ValueError("an employee cannot be their own manager")
            manager = db.get(Employee, new_manager_id)
            if manager is None or not manager.is_active:
                raise ValueError(f"manager_id {new_manager_id!r} is not an active employee")

    for field, raw in changes.items():
        setattr(target, field, _coerce(field, raw))
    db.commit()
    db.refresh(target)

    # full_name / preferred_name / job_title all feed build_profile_text.
    # Re-indexing on any change here rather than only on those three: the
    # cost is one request, and a list of "indexed fields" maintained by hand
    # in a second place is a rule 6 violation waiting to happen the next
    # time build_profile_text grows a line.
    reindex_employee(db, target)

    _audit(db, caller, "update_employee", f"person_id={person_id}", changes.keys())
    return target


# ---------------------------------------------------------------------------
# Maker-checker: restricting a profile, deactivating an employee, or adding
# one is staged as a request, not applied directly. The requester's OWN
# reporting chain resolves who has to approve it — never the target's chain
# (there isn't one for a create), and never the requester themselves. See
# _resolve_approver for the escalation rule (delegate first when away, then
# up one level, bounded and exhaustible).
# ---------------------------------------------------------------------------

def _resolve_approver(db: Session, requester_id: str) -> Employee | None:
    """Walks the REQUESTER's reporting chain, nearest first, for someone who
    can actually act right now. is_active is a hard requirement; away tries
    that manager's own delegate (the field already means "who's covering
    for me while I'm away" — this is exactly that use), then continues past
    them if the delegate isn't usable either. Bounded by
    org_chart.MAX_DEPTH, the same cycle guard every other chain walk in this
    app already uses. None (not a guess) if the whole chain is exhausted.
    """
    for candidate_id in manager_chain_ids(db, requester_id):
        candidate = db.get(Employee, candidate_id)
        if candidate is None or not candidate.is_active:
            continue
        if candidate.availability_status != AvailabilityStatus.away:
            return candidate
        if candidate.delegate_id:
            delegate = db.get(Employee, candidate.delegate_id)
            if delegate is not None and delegate.is_active and delegate.availability_status != AvailabilityStatus.away:
                return delegate
        # away, with no usable delegate — fall through to the next manager up
    return None


def _notify(db: Session, *, recipient_id: str, subject_employee_id: str, kind: NotificationKind, body: str) -> None:
    """Same "the row is the delivery" shape app/notifications.py's two
    triggers already use — reused directly rather than duplicated, since a
    real transport plugging in later should have one seam, not two."""
    db.add(Notification(
        recipient_id=recipient_id, subject_employee_id=subject_employee_id, course_id=None,
        kind=kind, display_status="", event_key=None, body=body, sequence=0, levels_up=0,
        created_at=datetime.now(),
    ))
    db.commit()


def _requester_label(caller: AuthenticatedUser) -> str:
    return caller.name or caller.id


def request_subject_name(db: Session, request: EmployeeActionRequest) -> str:
    """Who a request is about, in words, whether or not they exist yet.

    A `create` request's subject lives only in its payload until the moment
    it's approved, so every surface that wants to say "X requested to
    <action> <someone>" needs this rather than a plain db.get on
    target_employee_id. Public because app.main's serializer needs the same
    answer the notification bodies use — two spellings of "who is this
    about" would drift.
    """
    if request.target_employee_id is not None:
        target = db.get(Employee, request.target_employee_id)
        if target is not None:
            return target.full_name
        return request.target_employee_id
    if request.payload:
        proposed = json.loads(request.payload).get("full_name")
        if proposed:
            return proposed
    return "(unknown)"


def request_restriction(
    db: Session, caller: AuthenticatedUser, person_id: str, view_mode: ViewMode,
) -> EmployeeActionRequest:
    """Stages a restrict request; does not restrict anything. The actual
    availability_status flip only happens in approve_action_request, once
    the resolved approver acts."""
    _authorize(caller.role, view_mode, {"restrict_employee"})
    if person_id == caller.id:
        raise WriteDenied("an employee cannot restrict their own record")

    target = db.get(Employee, person_id)
    if target is None or not target.is_active:
        raise WriteTargetMissing(person_id)
    if target.availability_status == AvailabilityStatus.restricted:
        raise ValueError(f"employee {person_id} is already restricted")

    approver = _resolve_approver(db, caller.id)
    if approver is None:
        raise NoApproverAvailable(caller.id)

    request = EmployeeActionRequest(
        action_type=EmployeeActionType.restrict, target_employee_id=person_id,
        requested_by=caller.id, approver_id=approver.id,
        status=EmployeeActionStatus.pending, created_at=datetime.now(),
    )
    db.add(request)
    db.commit()
    db.refresh(request)

    _audit(db, caller, "request_restriction", f"person_id={person_id}",
           {"action_type", "target_employee_id", "approver_id"})
    _notify(
        db, recipient_id=approver.id, subject_employee_id=person_id,
        kind=NotificationKind.action_approval_requested,
        body=f"{_requester_label(caller)} requested to restrict {target.full_name}'s profile "
             f"— review and approve or reject.",
    )
    return request


def _active_direct_reports(db: Session, person_id: str) -> list[Employee]:
    return (
        db.query(Employee)
        .filter(Employee.manager_id == person_id, Employee.is_active == True)  # noqa: E712
        .all()
    )


def request_deactivation(
    db: Session, caller: AuthenticatedUser, person_id: str, view_mode: ViewMode,
) -> EmployeeActionRequest:
    """Stages a deactivate request; does not deactivate anything. Still
    blocked up front (409, immediate feedback) while the target manages
    anyone active — HR reassigns those people first via update_employee's
    manager_id field — and re-checked again at approval time in
    approve_action_request, since who reports to whom can change in the
    time an approval is pending.
    """
    _authorize(caller.role, view_mode, {"deactivate_employee"})
    if person_id == caller.id:
        raise WriteDenied("an employee cannot deactivate their own record")

    target = db.get(Employee, person_id)
    if target is None:
        raise WriteTargetMissing(person_id)
    if not target.is_active:
        raise EmployeeAlreadyInactive(person_id)

    active_reports = _active_direct_reports(db, person_id)
    if active_reports:
        raise HasActiveDirectReports([{"id": r.id, "full_name": r.full_name} for r in active_reports])

    approver = _resolve_approver(db, caller.id)
    if approver is None:
        raise NoApproverAvailable(caller.id)

    request = EmployeeActionRequest(
        action_type=EmployeeActionType.deactivate, target_employee_id=person_id,
        requested_by=caller.id, approver_id=approver.id,
        status=EmployeeActionStatus.pending, created_at=datetime.now(),
    )
    db.add(request)
    db.commit()
    db.refresh(request)

    _audit(db, caller, "request_deactivation", f"person_id={person_id}",
           {"action_type", "target_employee_id", "approver_id"})
    _notify(
        db, recipient_id=approver.id, subject_employee_id=person_id,
        kind=NotificationKind.action_approval_requested,
        body=f"{_requester_label(caller)} requested to deactivate {target.full_name} "
             f"— review and approve or reject.",
    )
    return request


def _apply_deactivation(db: Session, target: Employee) -> None:
    """The actual mutation, extracted so approve_action_request and (were
    there ever a second caller) share exactly one place that flips
    is_active. Delegate references are cleared unconditionally: `delegate`
    means "who's covering while away," and leaving it pointed at someone
    now deactivated is a straightforward cleanup, not a decision anyone
    needs to make by hand the way management reassignment is."""
    for employee in db.query(Employee).filter(
        Employee.delegate_id == target.id, Employee.is_active == True,  # noqa: E712
    ).all():
        employee.delegate_id = None

    target.is_active = False
    target.deactivated_at = datetime.now()
    db.commit()
    db.refresh(target)
    reindex_employee(db, target)


def approve_action_request(
    db: Session, caller: AuthenticatedUser, request_id: int, view_mode: ViewMode,
) -> EmployeeActionRequest:
    """Gated by IDENTITY (caller.id == request.approver_id), not by role —
    the resolved approver is whoever the requester's reporting chain
    actually names, which has nothing to do with which role header they
    happen to carry on this request. Re-validates the same preconditions
    request_deactivation checked up front, since the org can move in the
    time an approval sits pending."""
    request = db.get(EmployeeActionRequest, request_id)
    if request is None:
        raise WriteTargetMissing(str(request_id))
    if request.status is not EmployeeActionStatus.pending:
        raise RequestNotPending(f"request {request_id} is already {request.status.value}")
    if request.approver_id != caller.id:
        raise WriteDenied(f"only the resolved approver may act on request {request_id}")

    if request.action_type is EmployeeActionType.create:
        # No target to load — this branch MAKES the target. Re-validated
        # against the live database first (see _validate_create_fields):
        # between staging and now, the email could have been taken by
        # another hire, or the chosen manager or mentor could have left.
        payload = json.loads(request.payload) if request.payload else {}
        _validate_create_fields(db, payload)
        created = _apply_creation(db, payload)

        request.status = EmployeeActionStatus.approved
        request.resolved_at = datetime.now()
        request.resolved_by = caller.id
        # Now that the person exists, the request finally has something to
        # point at — so the row stops being the only record of who it was
        # about, and the created employee is reachable from the audit trail.
        request.target_employee_id = created.id
        db.commit()
        db.refresh(request)

        _audit(db, caller, "approve_action_request", f"request_id={request_id}",
               {"is_active", "target_employee_id"})
        _notify(
            db, recipient_id=request.requested_by, subject_employee_id=created.id,
            kind=NotificationKind.action_approved,
            body=f"Your request to add {created.full_name} to the directory was approved.",
        )
        return request

    target = db.get(Employee, request.target_employee_id)
    if target is None or not target.is_active:
        raise WriteTargetMissing(request.target_employee_id or "(no target)")

    if request.action_type is EmployeeActionType.deactivate:
        active_reports = _active_direct_reports(db, target.id)
        if active_reports:
            raise HasActiveDirectReports([{"id": r.id, "full_name": r.full_name} for r in active_reports])
        _apply_deactivation(db, target)
        fields = {"is_active", "deactivated_at"}
    else:
        if target.availability_status == AvailabilityStatus.restricted:
            raise ValueError(f"employee {target.id} is already restricted")
        target.availability_status = AvailabilityStatus.restricted
        db.commit()
        db.refresh(target)
        reindex_employee(db, target)
        fields = {"availability_status"}

    request.status = EmployeeActionStatus.approved
    request.resolved_at = datetime.now()
    request.resolved_by = caller.id
    db.commit()
    db.refresh(request)

    _audit(db, caller, "approve_action_request", f"request_id={request_id}", fields)
    _notify(
        db, recipient_id=request.requested_by, subject_employee_id=target.id,
        kind=NotificationKind.action_approved,
        body=f"Your request to {request.action_type.value} {target.full_name} was approved.",
    )
    return request


def reject_action_request(
    db: Session, caller: AuthenticatedUser, request_id: int, view_mode: ViewMode,
    reason: str | None = None,
) -> EmployeeActionRequest:
    request = db.get(EmployeeActionRequest, request_id)
    if request is None:
        raise WriteTargetMissing(str(request_id))
    if request.status is not EmployeeActionStatus.pending:
        raise RequestNotPending(f"request {request_id} is already {request.status.value}")
    if request.approver_id != caller.id:
        raise WriteDenied(f"only the resolved approver may act on request {request_id}")

    request.status = EmployeeActionStatus.rejected
    request.resolved_at = datetime.now()
    request.resolved_by = caller.id
    request.rejection_reason = reason
    db.commit()
    db.refresh(request)

    _audit(db, caller, "reject_action_request", f"request_id={request_id}", {"status"})
    reason_suffix = f" Reason: {reason}" if reason else ""
    _notify(
        db, recipient_id=request.requested_by,
        # A rejected create leaves no employee behind to point at, so the
        # notification is about the requester — same reasoning as the one
        # request_creation sends, and the only other real person involved.
        subject_employee_id=request.target_employee_id or request.requested_by,
        kind=NotificationKind.action_rejected,
        body=f"Your request to {request.action_type.value} {request_subject_name(db, request)} "
             f"was rejected.{reason_suffix}",
    )
    return request


def list_my_pending_approvals(db: Session, caller: AuthenticatedUser) -> list[EmployeeActionRequest]:
    """Every pending request — restrict, deactivate, create — this caller is
    the resolved approver for. No role gate at all, deliberately: the
    approver is resolved by reporting-chain identity (_resolve_approver),
    which has nothing to do with which role header this caller happens to be
    using right now."""
    return (
        db.query(EmployeeActionRequest)
        .filter(EmployeeActionRequest.approver_id == caller.id,
                EmployeeActionRequest.status == EmployeeActionStatus.pending)
        .order_by(EmployeeActionRequest.created_at)
        .all()
    )


def reactivate_employee(
    db: Session, caller: AuthenticatedUser, person_id: str, view_mode: ViewMode,
) -> Employee:
    """Sets is_active=True. Deliberately does not restore delegate
    references deactivate_employee cleared — those pointed at the target
    being unavailable to cover for someone else, not at the target's own
    employment, and re-establishing them silently would be guessing at a
    relationship HR never actually decided to recreate.

    No is_active check on the way in via db.get(): unlike every read path
    in this app, this function's whole job is to act on an inactive
    record, so app.people.get_person's "not found" gate for is_active=False
    would be exactly wrong here.
    """
    _authorize(caller.role, view_mode, {"deactivate_employee"})

    target = db.get(Employee, person_id)
    if target is None:
        raise WriteTargetMissing(person_id)
    if target.is_active:
        raise ValueError(f"employee {person_id} is already active")

    target.is_active = True
    target.deactivated_at = None
    db.commit()
    db.refresh(target)

    reindex_employee(db, target)
    _audit(db, caller, "reactivate_employee", f"person_id={person_id}", {"is_active", "deactivated_at"})
    return target


# How many deactivated employees the list returns, newest departure first.
# Capped because this set grows monotonically for the life of the company —
# every person ever deactivated stays in it — while the thing it exists to
# serve is short-horizon: undoing a mistaken deactivation, or reinstating a
# recent leaver. A genuine "search every former employee" need would want a
# query parameter rather than a bigger number here.
MAX_DEACTIVATED_RESULTS = 200

# The only fields the list surfaces. Deliberately narrower than PersonDetail:
# this answers "who did we deactivate, when, and should they come back",
# which needs identity and placement and nothing else. HR in work mode could
# read salary through the ordinary profile path anyway — the point is that
# this carve-out into is_active=False territory stays as small as it can be,
# not that the data is secret from this caller.
DEACTIVATED_FIELDS = frozenset({
    "id", "full_name", "job_title", "org_unit", "work_email", "deactivated_at",
})


def deactivated_employees_query():
    """The SELECT behind list_deactivated_employees, split out so it can be
    compiled against a dialect without a database to run it on.

    Exists because this query's one production failure was invisible to the
    test suite: it ran fine on SQLite and 500'd on Azure SQL. Handing the
    statement back lets a test compile it for mssql and assert on the SQL —
    see test_deactivated_ordering_compiles_for_sql_server.
    """
    return (
        select(Employee, OrgUnit)
        .outerjoin(OrgUnit, Employee.org_unit_id == OrgUnit.id)
        .where(Employee.is_active == False)  # noqa: E712
        # NULLs last without relying on dialect-specific NULLS LAST, which
        # SQLite accepts and older SQL Server does not: sort on a computed
        # "is it null" flag first, then the date itself.
        #
        # The flag has to be a CASE, not the bare `deactivated_at.is_(None)`
        # this used to be. SQLAlchemy renders that predicate verbatim as
        # `ORDER BY deactivated_at IS NULL`, which SQLite evaluates as 0/1
        # but T-SQL rejects outright — SQL Server has no boolean type, so a
        # predicate is not a sortable expression there. Same family as the
        # `== True` / `.is_(True)` note on the filter above: the local
        # SQLite suite cannot catch it, because the difference only exists
        # in the dialect.
        .order_by(
            case((Employee.deactivated_at.is_(None), 1), else_=0).asc(),
            Employee.deactivated_at.desc(),
        )
        .limit(MAX_DEACTIVATED_RESULTS)
    )


def list_deactivated_employees(
    db: Session, caller: AuthenticatedUser, view_mode: ViewMode,
) -> list[dict]:
    """Deactivated employees, most recently deactivated first.

    The one deliberate way to see is_active=False records at all. Every
    other read path in this app — find_people, get_person, the org chart,
    project membership, the search index — treats them as nonexistent for
    every caller including HR, which is what made reactivate_employee
    unreachable from the UI without knowing an id by heart. This is that
    gap closed, not that rule relaxed: it's one narrow list, gated by the
    same "deactivate_employee" capability that deactivating took in the
    first place, so the people who can put someone back are exactly the
    people who could have taken them out.

    Rows with deactivated_at NULL are included and sort last: an employee
    deactivated before that column existed (or seeded inactive) is still a
    deactivated employee, and dropping them from the only view that can
    see them would make them permanently unreachable.
    """
    _authorize(caller.role, view_mode, {"deactivate_employee"})

    rows = db.execute(deactivated_employees_query()).all()

    out = [
        {
            "id": employee.id,
            "full_name": employee.full_name,
            "job_title": employee.job_title,
            "org_unit": org_unit.name if org_unit else None,
            "work_email": employee.work_email,
            "deactivated_at": employee.deactivated_at,
        }
        for employee, org_unit in rows
    ]

    _audit(db, caller, "list_deactivated_employees", "(all deactivated)",
           DEACTIVATED_FIELDS, result_count=len(out))
    return out


# ---------------------------------------------------------------------------
# HR, work mode: create a new employee record — staged for approval, like
# restrict and deactivate. request_creation validates and parks the proposed
# fields; nothing exists in `employees` until the requester's own resolved
# approver approves and _apply_creation runs.
#
# Deliberately a small required set (full_name, job_title, org_unit_id,
# work_email, employment_type) plus a handful of optional placement fields
# (office_id, manager_id, preferred_name, work_phone, hire_date, mentor_id).
# Everything update_employee already covers -- salary, date_of_birth,
# cost_centre, linkedin_profile, and so on -- is reachable through that
# endpoint right after creation instead of duplicating its whole field set
# here. A new hire's basic identity and where they sit in the org is what
# onboarding actually needs on day one; the rest fills in as it becomes known.
#
# mentor_id is the one field that isn't an employees column at all: it
# becomes a community_links row (see _apply_creation), because "who shows
# this person the ropes" is a relationship, not an attribute of them.
# ---------------------------------------------------------------------------

_REQUIRED_CREATE_FIELDS = {"full_name", "job_title", "org_unit_id", "work_email", "employment_type"}
_OPTIONAL_CREATE_FIELDS = {
    "preferred_name", "office_id", "manager_id", "work_phone", "hire_date", "mentor_id",
}


def _validate_create_fields(db: Session, fields: dict) -> None:
    """Everything that must hold for a create to be applicable, with no
    writes and no side effects — so it can run twice: once when the request
    is staged (immediate feedback to HR, and an approver is never handed a
    request that cannot possibly apply) and again when the approval lands,
    because the world moves while a request sits pending. Somebody else can
    take the email address, the chosen manager or mentor can be deactivated,
    an org unit can be dissolved.
    """
    missing = _REQUIRED_CREATE_FIELDS - fields.keys()
    if missing:
        raise ValueError(f"missing required field(s): {', '.join(sorted(missing))}")
    unknown = fields.keys() - _REQUIRED_CREATE_FIELDS - _OPTIONAL_CREATE_FIELDS
    if unknown:
        raise ValueError(f"unknown field(s): {', '.join(sorted(unknown))}")

    if db.query(Employee).filter(Employee.work_email == fields["work_email"]).first() is not None:
        raise DuplicateEmail(fields["work_email"])

    if db.get(OrgUnit, fields["org_unit_id"]) is None:
        raise ValueError(f"org_unit_id {fields['org_unit_id']!r} does not exist")

    office_id = fields.get("office_id")
    if office_id is not None and db.get(Office, office_id) is None:
        raise ValueError(f"office_id {office_id!r} does not exist")

    # manager and mentor are both "must be a real, active person" — checked
    # the same way, reported separately, so HR is told which one went stale.
    for field in ("manager_id", "mentor_id"):
        person_id = fields.get(field)
        if person_id is None:
            continue
        person = db.get(Employee, person_id)
        if person is None or not person.is_active:
            raise ValueError(f"{field} {person_id!r} is not an active employee")

    # employment_type/hire_date parse errors surface here rather than at
    # apply time, where the approver would be the one seeing HR's typo.
    _coerce("employment_type", fields["employment_type"])
    _coerce("hire_date", fields.get("hire_date"))


def request_creation(
    db: Session, caller: AuthenticatedUser, fields: dict, view_mode: ViewMode,
) -> EmployeeActionRequest:
    """Stages a create request; creates nobody. The proposed fields are
    frozen into the request as JSON (see EmployeeActionRequest.payload for
    why a pending hire is not a half-built employees row), and only
    approve_action_request turns them into a person.
    """
    _authorize(caller.role, view_mode, {"create_employee"})
    _validate_create_fields(db, fields)

    approver = _resolve_approver(db, caller.id)
    if approver is None:
        raise NoApproverAvailable(caller.id)

    request = EmployeeActionRequest(
        action_type=EmployeeActionType.create, target_employee_id=None,
        payload=json.dumps(fields, default=str),
        requested_by=caller.id, approver_id=approver.id,
        status=EmployeeActionStatus.pending, created_at=datetime.now(),
    )
    db.add(request)
    db.commit()
    db.refresh(request)

    _audit(db, caller, "request_creation", f"work_email={fields['work_email']}",
           {"action_type", "payload", "approver_id"})
    _notify(
        # subject_employee_id is the REQUESTER here, not the proposed hire —
        # the column is a non-null FK to employees, and the whole point of
        # this request is that the person it describes has no row yet. The
        # requester is the only real employee this notification is about,
        # and the body carries the proposed name.
        db, recipient_id=approver.id, subject_employee_id=caller.id,
        kind=NotificationKind.action_approval_requested,
        body=f"{_requester_label(caller)} requested to add {fields['full_name']} "
             f"({fields['job_title']}) to the directory — review and approve or reject.",
    )
    return request


def _apply_creation(db: Session, payload: dict) -> Employee:
    """The actual insert, plus the mentor link if one was chosen. Takes no
    caller at all, same shape as _apply_deactivation: the approver is
    whoever the requester's reporting chain named, who may well not be HR,
    so re-running the create_employee capability check against THEIR role
    would deny the very approval that authorizes this.

    Callers must have run _validate_create_fields against the live database
    first — this function assumes the payload is applicable.
    """
    employee = Employee(
        full_name=payload["full_name"],
        preferred_name=payload.get("preferred_name"),
        job_title=payload["job_title"],
        org_unit_id=payload["org_unit_id"],
        office_id=payload.get("office_id"),
        manager_id=payload.get("manager_id"),
        work_email=payload["work_email"],
        work_phone=payload.get("work_phone"),
        employment_type=_coerce("employment_type", payload["employment_type"]),
        hire_date=_coerce("hire_date", payload.get("hire_date")) or date.today(),
        availability_status=AvailabilityStatus.available,
        is_active=True,
    )
    db.add(employee)
    db.commit()
    db.refresh(employee)

    mentor_id = payload.get("mentor_id")
    if mentor_id is not None:
        # Byte-for-byte the shape app.community_links.auto_assign_mentors
        # creates, on purpose: an HR-chosen mentor and a swept-in one must
        # be the same kind of row, or _resolve_mentor_expiration would age
        # them differently and _eligible_new_hires would not recognize this
        # one as "already has a mentor" — which is what stops the sweep from
        # later assigning a second mentor over the top of HR's choice.
        db.add(CommunityLink(
            owner_employee_id=employee.id, contact_employee_id=mentor_id,
            role_label="mentor", reason=None, source=CommunityLinkSource.official,
            office_id=employee.office_id, department_id=employee.org_unit_id,
            is_mentor_link=True, created_at=datetime.now(),
        ))
        db.commit()

    # Rule 6 — a brand-new employee is indexed the same as any other write
    # that touches build_profile_text's inputs (full_name, job_title, ...).
    reindex_employee(db, employee)
    return employee


# ---------------------------------------------------------------------------
# IT, work mode: CRUD on project descriptions.
#
# "CRUD on project entries" scoped to the description field specifically —
# EDITABLE gives it exactly {"project_desc"}, so creating or deleting the
# Project row itself (which would take name, type, classification, owner,
# owning unit — none of them editable by it) is out of scope by the same
# table that governs the edits. Remove therefore clears the description; it
# does not delete a project out from under the people staffed on it.
# ---------------------------------------------------------------------------

def set_project_description(
    db: Session, caller: AuthenticatedUser, project_id: int, description: str | None,
    view_mode: ViewMode, source: str | None = None,
) -> Project:
    _authorize(caller.role, view_mode, {"project_desc"})

    project = db.get(Project, project_id)
    if project is None:
        raise WriteTargetMissing(str(project_id))

    project.description = description
    db.commit()
    db.refresh(project)

    # Project descriptions are not in build_profile_text today, but project
    # NAMES are, and everyone staffed on the project has the project in
    # their profile text. Re-indexing the members keeps rule 6 true by the
    # letter rather than by the accident of which project fields the
    # profile text currently happens to include.
    _reindex_project_members(db, project_id)

    _audit(db, caller, "set_project_description", f"project_id={project_id}",
           {"project_desc"}, source=source)
    return project


def clear_project_description(
    db: Session, caller: AuthenticatedUser, project_id: int, view_mode: ViewMode
) -> Project:
    """The 'remove' of the CRUD set. Separate function rather than a null
    through set_project_description so the audit trail distinguishes "IT
    wrote an empty description" from "IT removed the description"."""
    _authorize(caller.role, view_mode, {"project_desc"})

    project = db.get(Project, project_id)
    if project is None:
        raise WriteTargetMissing(str(project_id))

    project.description = None
    db.commit()
    db.refresh(project)
    _reindex_project_members(db, project_id)
    _audit(db, caller, "clear_project_description", f"project_id={project_id}", {"project_desc"})
    return project


def _reindex_project_members(db: Session, project_id: int) -> None:
    member_ids = [
        row.employee_id for row in
        db.query(EmployeeProject).filter(EmployeeProject.project_id == project_id).all()
    ]
    for employee_id in member_ids:
        reindex_employee_id(db, employee_id)

    # The project's own Mode 3 embedding is derived from its description, so
    # editing that description makes the stored vector stale in exactly the
    # way an employee's profile_text goes stale above. Same call, one row.
    #
    # Degrades rather than fails: reindex_project() returns False when the
    # embedding endpoint is unreachable, leaving the previous vector in
    # place. That's a stale corpus entry, not a broken write — and
    # source_hash makes the staleness detectable, so the next
    # build_project_embeddings.py run repairs it.
    reindex_project(db, project_id)
