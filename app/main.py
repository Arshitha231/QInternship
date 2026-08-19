from contextlib import asynccontextmanager
import json
from datetime import date
from pathlib import Path
from typing import AsyncIterator, Literal

from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from app import config
from app.auth import AuthenticatedUser, assert_dev_auth_is_intentional, get_current_user
from app.certifications import LocalStatusWritesDisabled, RecordCourseStatusDenied, UnknownCourse, record_course_status
from app.community_links import LinkDenied, LinkNotFound, SuggestionDenied, SuggestionNotActionable, SuggestionNotFound
from app.community_links import auto_assign_mentors as auto_assign_mentors_service
from app.community_links import confirm_suggested_official_link as confirm_suggested_official_link_service
from app.community_links import create_personal_link as create_personal_link_service
from app.community_links import delete_personal_link as delete_personal_link_service
from app.community_links import generate_suggested_official_links as generate_suggested_official_links_service
from app.community_links import list_community_links as list_community_links_service
from app.community_links import list_suggested_official_links as list_suggested_official_links_service
from app.community_links import reject_suggested_official_link as reject_suggested_official_link_service
from app.community_links import update_personal_link as update_personal_link_service
from app.continuity import AuthorizationRecordNotActionable, AuthorizationRecordNotFound
from app.continuity import acknowledge_hr_review as acknowledge_hr_review_service
from app.continuity import confirm_authorization_record as confirm_authorization_record_service
from app.continuity import get_employee_continuity as get_employee_continuity_service
from app.continuity import get_engagement_exposure as get_engagement_exposure_service
from app.continuity import get_hr_review_queue as get_hr_review_queue_service
from app.continuity import get_org_exposure as get_org_exposure_service
from app.continuity import reject_authorization_record as reject_authorization_record_service
from app.continuity import submit_authorization_record as submit_authorization_record_service
from app.db import engine, get_db
from app.demo_auth import DemoLoginDenied, DemoLoginDisabled, login as demo_login
from app.doc_extraction import UnsupportedDocument, process_document, store_document
from app.models import DocSubjectMatch, Employee, Office, OrgUnit, TrainingCourse
from app.models.enums import CourseStatus, display_status
from app.notifications import (
    NotifyDateMilestonesDenied, NotifyHrReviewsDenied, notifications_for, notify_date_milestones,
    notify_upcoming_hr_reviews,
)
from app.org_chart import get_org_chain as get_org_chain_service
from app.people import find_people as find_people_service
from app.people import get_person as get_person_service
from app.people import update_own_bio as update_own_bio_service
from app.people import update_own_name_pronunciation as update_own_name_pronunciation_service
from app.permissions import ViewMode, effective_role, resolve_view_mode
from app.project_skills import ProjectNotWritable, UnknownSkill
from app.project_skills import get_required_skills as get_required_skills_service
from app.project_skills import set_required_skills as set_required_skills_service
from app.proposals import (
    DocumentAlreadyFinalized,
    DocumentNotFound,
    ProposalNotActionable,
    ProposalNotFound,
    ReviewDenied,
    SubjectNotFound,
)
from app.proposals import accept as accept_proposal
from app.proposals import bulk_accept as bulk_accept_proposals
from app.proposals import bulk_reject as bulk_reject_proposals
from app.proposals import correct as correct_proposal
from app.proposals import edit as edit_proposal
from app.proposals import finalize_document as finalize_document_service
from app.proposals import list_documents
from app.proposals import list_proposals
from app.proposals import list_subjects
from app.proposals import reassign as reassign_proposal
from app.proposals import reject as reject_proposal
from app.proposals import resolve_subject
from app.proposals import undo as undo_proposal
from app.registry import assert_registry_covers_schema
from app.schemas import (
    AskRequest,
    AuthorizationRecordOut,
    BulkProposalRequest,
    CommunityLinkOut,
    ContinuityOverview,
    CorrectProposalRequest,
    CreateCommunityLinkRequest,
    CreateEmployeeRequest,
    EditProposalRequest,
    EmployeeContinuityDetail,
    EngagementExposure,
    FinalizeDocumentRequest,
    HrReviewQueueItem,
    LoginRequest,
    NotificationOut,
    OfficeOut,
    OrgChainNode,
    OrgUnitOut,
    PersonDetail,
    PersonRef,
    PersonSummary,
    ProjectDescriptionRequest,
    ProjectSkillRequirementIn,
    ProjectSkillRequirementOut,
    ReassignProposalRequest,
    RecordCourseStatusRequest,
    RejectActionRequestBody,
    ResolveSubjectRequest,
    SubmitAuthorizationRecordRequest,
    SuggestedOfficialLinkOut,
    UpdateBioRequest,
    UpdateCommunityLinkRequest,
    UpdateEmployeeRequest,
    UpdateNamePronunciationRequest,
)
from app.tool_calling import answer as answer_service
from app.unified_search import unified_search
from app.writes import (
    DuplicateEmail,
    EmployeeAlreadyInactive,
    HasActiveDirectReports,
    NoApproverAvailable,
    RequestNotPending,
    WriteDenied,
    WriteTargetMissing,
)
from app.writes import approve_action_request as approve_action_request_service
from app.writes import clear_project_description as clear_project_description_service
from app.writes import list_deactivated_employees as list_deactivated_employees_service
from app.writes import list_my_pending_approvals as list_pending_approvals_service
from app.writes import reactivate_employee as reactivate_employee_service
from app.writes import reject_action_request as reject_action_request_service
from app.writes import request_creation as request_creation_service
from app.writes import request_deactivation as request_deactivation_service
from app.writes import request_restriction as request_restriction_service
from app.writes import request_subject_name
from app.writes import set_project_description as set_project_description_service
from app.writes import update_employee as update_employee_service


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    # Fails loudly at startup if a DB column has no app/registry.py entry
    # and isn't in IGNORED_COLUMNS -- the same protection
    # assert_registry_covers_schema's own docstring describes: a teammate
    # adding employee.home_address must add a registry entry (or a
    # justified ignore) before it can ever become queryable, not after.
    assert_registry_covers_schema(engine)
    # Same shape: fails loudly if AUTH_MODE fell back to "dev" by omission
    # rather than by a deliberate ALLOW_DEV_AUTH=1 / AUTH_MODE=dev choice —
    # see assert_dev_auth_is_intentional's docstring for why that fallback
    # is a full auth bypass, not a degraded feature.
    assert_dev_auth_is_intentional()
    yield


app = FastAPI(
    title="Employee Directory API",
    description="Internal employee directory with permission-filtered natural-language search.",
    version="0.1.0",
    lifespan=_lifespan,
)

# Local frontend dev server only (Vite default port) — the API has no
# cookie-based session to protect against CSRF here, auth is a header the
# browser never attaches automatically.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict:
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql("SELECT 1")
        db_status = "ok"
    except Exception:
        db_status = "unreachable"
    return {"status": "ok" if db_status == "ok" else "degraded", "database": db_status}


@app.get("/auth/whoami", response_model=AuthenticatedUser)
def whoami(user: AuthenticatedUser = Depends(get_current_user)) -> AuthenticatedUser:
    return user


# --- Demo login. Dev mode only; 404s once real auth is configured, because
# the credentials it checks are a stand-in for the Entra app-role assignment
# and not an alternative to it. Any active employee may sign in, with the role
# derived from the org tree. See app/demo_auth.py.

@app.post("/auth/login", response_model=AuthenticatedUser)
def login_route(body: LoginRequest, db: Session = Depends(get_db)) -> AuthenticatedUser:
    try:
        return demo_login(db, body.email, body.password)
    except DemoLoginDisabled:
        raise HTTPException(status_code=404, detail="Not found")
    except DemoLoginDenied:
        raise HTTPException(status_code=401, detail="Incorrect email or password")


@app.get("/people", response_model=list[PersonSummary], response_model_exclude_unset=True)
def list_people(
    name: str | None = Query(
        None, description="Exact or partial person name. Keyword+prefix and fuzzy "
                          "(misspelling-tolerant) matching via hybrid search."),
    query: str | None = Query(
        None, description='Free-text description of a person, e.g. "who knows Power BI '
                          'in Bangalore". Routed through the same hybrid keyword+fuzzy+vector '
                          "search as `name`, plus a semantic (vector) match — use this for a "
                          "description rather than a literal name. Takes priority over `name` "
                          "if both are given."),
    skill: str | None = None,
    level: str | None = None,
    org_unit: str | None = None,
    office: str | None = None,
    language: str | None = None,
    available: bool | None = None,
    view_mode: str | None = Query(
        None, description='Which lens to read the directory through: "work" (the '
                          'caller\'s full privileges) or "employee" (what an ordinary '
                          'colleague sees). Only hr and it may choose; every other role '
                          'is answered in employee mode whatever it sends. Defaults to '
                          '"work" for hr/it.'),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[PersonSummary]:
    return find_people_service(
        db, user, name=name, query=query, skill=skill, level=level, org_unit=org_unit,
        office=office, language=language, available=available,
        view_mode=resolve_view_mode(user.role, view_mode),
    )


@app.get("/search")
def unified_search_route(
    q: str | None = Query(None, description="Free-text query or natural-language question."),
    skill: str | None = None,
    level: str | None = None,
    org_unit: str | None = None,
    office: str | None = None,
    language: str | None = None,
    available: bool | None = None,
    view_mode: str | None = Query(
        None, description='"work" or "employee" — see GET /people. Applies to the '
                          'assisted path too, so an hr/it caller in employee mode gets '
                          'employee-mode data whether they searched or asked a question.'),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict:
    """The merged Search+Ask entry point. Deterministically decides direct
    (structured, zero model calls) vs assisted (routed through the same
    tool-calling layer /ask uses) — see app.unified_search for the actual
    router. /people and /ask both stay in place unchanged underneath this;
    nothing here duplicates their retrieval or permission logic.

    No response_model here (the shape is a discriminated union, direct vs
    assisted) — so results/citations are dumped with exclude_unset by hand
    below, matching what response_model_exclude_unset does for /people. A
    field like direct_reports is only ever set on a PersonSummary instance
    for a manager/hr caller in the first place (see people.py); without
    this, FastAPI's default dict encoding would serialize every unset
    field as an explicit `null` instead of leaving the key genuinely
    absent — quietly telling a non-manager caller "this field exists, you
    just can't see it", the exact boundary-leak /people's flag exists to
    prevent.
    """
    result = unified_search(
        db, user, q=q,
        filters={"skill": skill, "level": level, "org_unit": org_unit,
                 "office": office, "language": language, "available": available},
        view_mode=resolve_view_mode(user.role, view_mode),
    )
    result["results"] = [p.model_dump(exclude_unset=True) for p in result["results"]]
    if result.get("overview") is not None:
        result["overview"]["citations"] = [c.model_dump(exclude_unset=True) for c in result["overview"]["citations"]]
    return result


@app.get("/people/{person_id}", response_model=PersonDetail, response_model_exclude_unset=True)
def get_person_route(
    person_id: str,
    view_mode: str | None = Query(
        None, description='"work" or "employee" — see GET /people. Forced to '
                          '"employee" for any role other than hr/it.'),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> PersonDetail:
    person = get_person_service(db, user, person_id, resolve_view_mode(user.role, view_mode))
    if person is None:
        # Identical response whether nobody matched or the caller lacks
        # access — redact, never reject.
        raise HTTPException(status_code=404, detail="Person not found")
    return person


@app.patch("/people/{person_id}/bio", response_model=PersonDetail, response_model_exclude_unset=True)
def update_bio_route(
    person_id: str,
    body: UpdateBioRequest,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> PersonDetail:
    # Self-service only — editing anyone else's About, even your own
    # direct reports', is out of scope for this endpoint.
    if person_id != user.id:
        raise HTTPException(status_code=403, detail="You can only edit your own profile")
    result = update_own_bio_service(db, user, person_id, body.bio.strip())
    if result is None:
        raise HTTPException(status_code=404, detail="Person not found")
    return result


@app.patch("/people/{person_id}/pronunciation", response_model=PersonDetail, response_model_exclude_unset=True)
def update_name_pronunciation_route(
    person_id: str,
    body: UpdateNamePronunciationRequest,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> PersonDetail:
    # Self-service only, same reasoning as /people/{person_id}/bio above:
    # how your own name sounds isn't something anyone else edits on your
    # behalf, not even HR.
    if person_id != user.id:
        raise HTTPException(status_code=403, detail="You can only edit your own profile")
    result = update_own_name_pronunciation_service(db, user, person_id, body.name_pronunciation.strip())
    if result is None:
        raise HTTPException(status_code=404, detail="Person not found")
    return result


@app.patch("/employees/{person_id}", response_model=PersonDetail, response_model_exclude_unset=True)
def update_employee_route(
    person_id: str,
    body: UpdateEmployeeRequest,
    view_mode: str | None = Query(None, description='"work" or "employee" — see GET /people.'),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> PersonDetail:
    """HR, work mode: edit internal fields on any employee.

    The role and view_mode gate is applied by the service against the
    EDITABLE table, not here — this route only resolves the mode and
    translates the service's exceptions into status codes. Calling it
    directly with view_mode=work as an employee is refused server-side; the
    UI hiding the form is not what stops it.
    """
    mode = resolve_view_mode(user.role, view_mode)
    changes = body.model_dump(exclude_unset=True)
    try:
        update_employee_service(db, user, person_id, changes, mode)
    except WriteDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except WriteTargetMissing as exc:
        raise HTTPException(status_code=404, detail="Person not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Re-read through the ordinary permission-filtered path, so the response
    # shows what this caller may see rather than what they just wrote.
    person = get_person_service(db, user, person_id, mode)
    if person is None:
        raise HTTPException(status_code=404, detail="Person not found")
    return person


def _employee_action_result(employee) -> dict:
    """Shared response shape for deactivate/reactivate/create — a plain
    summary, not PersonDetail. update_employee_route's own re-read-through-
    get_person pattern doesn't work here: get_person returns None for
    is_active=False for every caller including HR (app.people.get_person's
    own retrieval gate), which is exactly the state deactivate/reactivate
    are transitioning through."""
    return {
        "id": employee.id,
        "full_name": employee.full_name,
        "job_title": employee.job_title,
        "is_active": employee.is_active,
        "availability_status": employee.availability_status.value,
        "deactivated_at": employee.deactivated_at,
    }


@app.get("/org_units", response_model=list[OrgUnitOut])
def list_org_units_route(
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[OrgUnit]:
    """Every org unit, flat, for any authenticated caller. Not sensitive —
    org_unit is already in BASE_FIELDS, visible on every profile regardless
    of role — so this lists the structure itself for the create-employee
    picker rather than gating it further than the data it's built from
    already is. get_current_user is still the auth gate — any authenticated
    caller, no additional role check needed beyond it."""
    return db.query(OrgUnit).order_by(OrgUnit.name).all()


@app.get("/offices", response_model=list[OfficeOut])
def list_offices_route(
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[Office]:
    """Every office. Same non-sensitivity reasoning as /org_units — office
    is already in BASE_FIELDS."""
    return db.query(Office).order_by(Office.name).all()


@app.post("/employees", status_code=202)
def create_employee_route(
    body: CreateEmployeeRequest,
    view_mode: str | None = Query(None, description='"work" or "employee" — see GET /people.'),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict:
    """HR, work mode: stages a request to add an employee — creates nobody.
    202, not 201: the response describes a pending approval, and there is no
    person to have created yet. Only approve_action_request, called by the
    requester's own resolved approver, inserts the row. See
    app.writes.request_creation for the required/optional field split and
    for how mentor_id becomes a community link."""
    mode = resolve_view_mode(user.role, view_mode)
    fields = body.model_dump(exclude_unset=True)
    try:
        request = request_creation_service(db, user, fields, mode)
    except WriteDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except DuplicateEmail as exc:
        raise HTTPException(status_code=409, detail=f"work_email {exc} is already in use") from exc
    except NoApproverAvailable as exc:
        raise HTTPException(
            status_code=422, detail=f"no reachable approver in {exc}'s reporting chain") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _action_request_result(db, request)


def _action_request_result(db: Session, request) -> dict:
    approver = db.get(Employee, request.approver_id) if request.approver_id else None
    requester = db.get(Employee, request.requested_by)
    return {
        "request_id": request.id,
        "action_type": request.action_type.value,
        "status": request.status.value,
        # Null until approval for a `create` request — there is no employee
        # id to report while the person is still only proposed. target_name
        # is always populated, from the payload in that case (see
        # app.writes.request_subject_name).
        "target_id": request.target_employee_id,
        "target_name": request_subject_name(db, request),
        "approver_id": request.approver_id,
        "approver_name": approver.full_name if approver else None,
        "requested_by": request.requested_by,
        # Names, not just ids: the approval list is read by a person
        # deciding whether to approve, and "8f3c-..." requested to
        # deactivate someone tells them nothing about who is asking.
        "requested_by_name": requester.full_name if requester else request.requested_by,
        "created_at": request.created_at,
        "resolved_at": request.resolved_at,
        "rejection_reason": request.rejection_reason,
    }


@app.post("/employees/{person_id}/restrict")
def restrict_employee_route(
    person_id: str,
    view_mode: str | None = Query(None, description='"work" or "employee" — see GET /people.'),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict:
    """HR, work mode: stages a restrict request — does not restrict the
    profile. Only approve_action_request, called by the requester's own
    resolved approver, actually applies it. See app.writes.request_restriction
    for how the approver is chosen."""
    mode = resolve_view_mode(user.role, view_mode)
    try:
        request = request_restriction_service(db, user, person_id, mode)
    except WriteDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except WriteTargetMissing as exc:
        raise HTTPException(status_code=404, detail="Person not found") from exc
    except NoApproverAvailable as exc:
        raise HTTPException(
            status_code=422, detail=f"no reachable approver in {exc}'s reporting chain") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _action_request_result(db, request)


@app.post("/employees/{person_id}/deactivate")
def deactivate_employee_route(
    person_id: str,
    view_mode: str | None = Query(None, description='"work" or "employee" — see GET /people.'),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict:
    """HR, work mode: stages a deactivate request — does not deactivate the
    employee. Blocked (409) up front while the target still manages anyone
    active; only approve_action_request actually flips is_active. See
    app.writes.request_deactivation."""
    mode = resolve_view_mode(user.role, view_mode)
    try:
        request = request_deactivation_service(db, user, person_id, mode)
    except WriteDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except WriteTargetMissing as exc:
        raise HTTPException(status_code=404, detail="Person not found") from exc
    except EmployeeAlreadyInactive as exc:
        raise HTTPException(status_code=409, detail=f"{exc} is already inactive") from exc
    except HasActiveDirectReports as exc:
        raise HTTPException(status_code=409, detail={
            "message": str(exc), "active_direct_reports": exc.reports,
        }) from exc
    except NoApproverAvailable as exc:
        raise HTTPException(
            status_code=422, detail=f"no reachable approver in {exc}'s reporting chain") from exc
    return _action_request_result(db, request)


@app.get("/employee_action_requests")
def list_pending_approvals_route(
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict:
    """Every pending restrict/deactivate/create request THIS caller is the
    resolved approver for — identity-gated (app.writes.list_my_pending_approvals),
    not role-gated. Whoever the requester's reporting chain names as
    approver sees these, whatever role header they're currently using."""
    requests = list_pending_approvals_service(db, user)
    return {"requests": [_action_request_result(db, r) for r in requests]}


@app.post("/employee_action_requests/{request_id}/approve")
def approve_action_request_route(
    request_id: int,
    view_mode: str | None = Query(None, description='"work" or "employee" — see GET /people.'),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict:
    """Only the request's own resolved approver may call this — see
    app.writes.approve_action_request.

    The 409s below are all the same situation: the request was valid when it
    was staged and no longer is, because the org moved while it sat pending.
    A create whose email has since been taken, a deactivation whose target
    has picked up direct reports — both refuse the whole action rather than
    applying a partial one."""
    mode = resolve_view_mode(user.role, view_mode)
    try:
        request = approve_action_request_service(db, user, request_id, mode)
    except WriteDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except WriteTargetMissing as exc:
        raise HTTPException(status_code=404, detail="Request or target not found") from exc
    except RequestNotPending as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DuplicateEmail as exc:
        raise HTTPException(
            status_code=409, detail=f"work_email {exc} was taken while this request was pending",
        ) from exc
    except HasActiveDirectReports as exc:
        raise HTTPException(status_code=409, detail={
            "message": str(exc), "active_direct_reports": exc.reports,
        }) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _action_request_result(db, request)


@app.post("/employee_action_requests/{request_id}/reject")
def reject_action_request_route(
    request_id: int,
    body: RejectActionRequestBody,
    view_mode: str | None = Query(None, description='"work" or "employee" — see GET /people.'),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict:
    """Only the request's own resolved approver may call this — see
    app.writes.reject_action_request."""
    mode = resolve_view_mode(user.role, view_mode)
    try:
        request = reject_action_request_service(db, user, request_id, mode, reason=body.reason)
    except WriteDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except WriteTargetMissing as exc:
        raise HTTPException(status_code=404, detail="Request not found") from exc
    except RequestNotPending as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _action_request_result(db, request)


@app.get("/employees/deactivated")
def list_deactivated_employees_route(
    view_mode: str | None = Query(None, description='"work" or "employee" — see GET /people.'),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict:
    """HR, work mode: the deactivated employees, newest departure first.

    The only route in this app that surfaces is_active=False records —
    every other read treats them as nonexistent, which is what left
    /employees/{id}/reactivate unreachable without knowing an id by heart.
    Gated by the same capability deactivating took. Static path, so no
    collision with the /employees/{person_id}/... routes below.
    """
    mode = resolve_view_mode(user.role, view_mode)
    try:
        employees = list_deactivated_employees_service(db, user, mode)
    except WriteDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return {"employees": employees}


@app.post("/employees/{person_id}/reactivate")
def reactivate_employee_route(
    person_id: str,
    view_mode: str | None = Query(None, description='"work" or "employee" — see GET /people.'),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict:
    """HR, work mode: reverse a deactivation."""
    mode = resolve_view_mode(user.role, view_mode)
    try:
        employee = reactivate_employee_service(db, user, person_id, mode)
    except WriteDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except WriteTargetMissing as exc:
        raise HTTPException(status_code=404, detail="Person not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _employee_action_result(employee)


@app.put("/projects/{project_id}/description")
def set_project_description_route(
    project_id: int,
    body: ProjectDescriptionRequest,
    view_mode: str | None = Query(None, description='"work" or "employee" — see GET /people.'),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict:
    """IT, work mode: add or edit a project description."""
    mode = resolve_view_mode(user.role, view_mode)
    try:
        project = set_project_description_service(db, user, project_id, body.description, mode)
    except WriteDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except WriteTargetMissing as exc:
        raise HTTPException(status_code=404, detail="Project not found") from exc
    return {"project_id": project.id, "project_name": project.name,
            "project_desc": project.description}


@app.delete("/projects/{project_id}/description")
def clear_project_description_route(
    project_id: int,
    view_mode: str | None = Query(None, description='"work" or "employee" — see GET /people.'),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict:
    """IT, work mode: remove a project description.

    Removes the description, not the project — see app/writes.py for why
    deleting the Project row is out of scope for a role whose editable set
    is exactly {project_desc}.
    """
    mode = resolve_view_mode(user.role, view_mode)
    try:
        project = clear_project_description_service(db, user, project_id, mode)
    except WriteDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except WriteTargetMissing as exc:
        raise HTTPException(status_code=404, detail="Project not found") from exc
    return {"project_id": project.id, "project_name": project.name, "project_desc": None}


@app.get("/people/{person_id}/org-chart", response_model=list[OrgChainNode])
def get_org_chart_route(
    person_id: str,
    direction: Literal["up", "down"] = "up",
    depth: int = 10,
    view_mode: str | None = Query(None, description='"work" or "employee" — see GET /people.'),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[OrgChainNode]:
    """The manager chain in either direction. Downward is manager/hr in work
    mode only — this route had no view_mode parameter at all until now, so
    employee mode could not be expressed here even though every other read
    route honours it."""
    result = get_org_chain_service(
        db, user, person_id, direction, depth, resolve_view_mode(user.role, view_mode))
    if result is None:
        # Same identical-shape rule as get_person: root not visible or not
        # found look the same. Direction access (downward, wrong role) is a
        # different case — that's an empty list, handled inside the service.
        raise HTTPException(status_code=404, detail="Person not found")
    return result


@app.get("/me/notifications", response_model=list[NotificationOut])
def my_notifications_route(
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[NotificationOut]:
    """Your own notifications, newest first.

    Keyed on the caller's id with no person_id parameter at all — there is
    deliberately no route shape that could read someone else's inbox, for
    any role. An hr caller reading a manager's course reports would be a
    second, unaudited way to learn who failed what.
    """
    out: list[NotificationOut] = []
    for n in notifications_for(db, user.id):
        subject = db.get(Employee, n.subject_employee_id)
        course = db.get(TrainingCourse, n.course_id)
        out.append(NotificationOut(
            id=n.id, kind=n.kind.value,
            subject_person=PersonRef(
                id=n.subject_employee_id,
                full_name=subject.full_name if subject else "",
            ),
            course_name=course.name if course else "",
            display_status=n.display_status, body=n.body,
            levels_up=n.levels_up, created_at=n.created_at,
        ))
    return out


@app.post("/people/{person_id}/training/{course_code}", status_code=201)
def record_training_status_route(
    person_id: str,
    course_code: str,
    body: RecordCourseStatusRequest,
    view_mode: str | None = Query(None, description='"work" or "employee" — see GET /people.'),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict:
    """Record a course status change and fire both notification triggers.

    Stands in for the training system telling us something happened, which
    is the event neither provider can invent — it's what makes the triggers
    demoable while ENABLE_TRAINING_API_SYNC is off. When the push-vs-pull
    question is settled with the other team, their webhook handler calls the
    same service function; this route stays as the manual/backfill path.

    hr-only: it is the one inbound surface that speaks the four-value status,
    and an employee marking their own course completed would make the whole
    pipeline decorative. 409 once real sync is on — see
    LocalStatusWritesDisabled.
    """
    # effective_role, not user.role: every other write in this app is
    # impossible in employee mode because EDITABLE[(role, "employee")] is
    # empty, and this route bypasses that table with its own check. Without
    # the collapse it would be the one write an hr caller could still make
    # while previewing the ordinary-colleague view.
    if effective_role(user.role, resolve_view_mode(user.role, view_mode)) != "hr":
        raise HTTPException(
            status_code=403, detail="Recording course status is an HR action, in work mode")

    employee = db.get(Employee, person_id)
    if employee is None or not employee.is_active:
        raise HTTPException(status_code=404, detail="Person not found")

    try:
        row, notifications = record_course_status(
            db, caller=user, employee=employee, course_code=course_code,
            status=CourseStatus(body.status),
            attempted_on=body.attempted_on, completed_on=body.completed_on,
        )
    except LocalStatusWritesDisabled as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except UnknownCourse as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RecordCourseStatusDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    # The four-value status echoes back here — this endpoint's caller is hr
    # and already sent it. It stays out of every profile response.
    return {
        "employee_id": row.employee_id,
        "course_code": course_code,
        "status": row.status.value,
        "display_status": display_status(row.status).value,
        "notifications_sent": len(notifications),
    }


@app.post("/notifications/date-milestones", status_code=201)
def run_date_milestones_route(
    on: date | None = Query(None, description="Date to sweep, YYYY-MM-DD. Defaults to today."),
    view_mode: str | None = Query(None, description='"work" or "employee" — see GET /people.'),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict:
    """Sweep for birthdays and milestone service anniversaries, notifying HR.

    A sweep rather than an event: nothing changes in the database on
    someone's birthday, so something has to come looking. This project has no
    scheduler, so this route is what a daily cron or Azure timer would call —
    the logic lives in app/notifications.py and doesn't care what invoked it.

    Idempotent, so a retried cron or a doubled-up timer can't produce a second
    birthday message; re-running for the same date returns `notifications_sent:
    0`. `on` exists so a past or future date can be swept deliberately, which
    is also the only way to demo it without waiting for a real birthday.

    hr-only: it writes notifications to HR's own inboxes on everyone's behalf.
    """
    # Same reasoning as the training route above: a bare role check here
    # would be the exception to EDITABLE's "nothing is writable in employee
    # mode" rule.
    if effective_role(user.role, resolve_view_mode(user.role, view_mode)) != "hr":
        raise HTTPException(
            status_code=403, detail="Running the date sweep is an HR action, in work mode")

    on_date = on or date.today()
    try:
        created = notify_date_milestones(db, user, on_date)
    except NotifyDateMilestonesDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    db.commit()

    by_kind: dict[str, int] = {}
    for n in created:
        by_kind[n.kind.value] = by_kind.get(n.kind.value, 0) + 1
    return {
        "date": on_date.isoformat(),
        "notifications_sent": len(created),
        "by_kind": by_kind,
    }


@app.post("/notifications/hr-review-reminders", status_code=201)
def run_hr_review_reminders_route(
    on: date | None = Query(None, description="Date to sweep, YYYY-MM-DD. Defaults to today."),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict:
    """Sweep for work-authorization reviews due soon, notifying HR.

    Same shape as POST /notifications/date-milestones — a sweep, not an
    event, for the same reason (nothing changes in the database when a
    review date approaches). Only ever reminds about a record HR hasn't
    silenced via POST /continuity/review-queue/{record_id}/acknowledge —
    see app.notifications.notify_upcoming_hr_reviews. HR-only.
    """
    if user.role != "hr":
        raise HTTPException(status_code=403, detail="Running the HR-review sweep is an HR action")

    on_date = on or date.today()
    try:
        created = notify_upcoming_hr_reviews(db, user, on_date)
    except NotifyHrReviewsDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    db.commit()

    return {
        "date": on_date.isoformat(),
        "notifications_sent": len(created),
    }


@app.get("/projects/{project_id}/required-skills", response_model=list[ProjectSkillRequirementOut])
def get_required_skills_route(
    project_id: int,
    view_mode: str | None = Query(None, description='"work" or "employee" — see GET /people.'),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[ProjectSkillRequirementOut]:
    """What a project's delivery actually needs, per app/project_skills.py.
    Not sensitive — visible to anyone who can see the project at all
    (confidential projects: members and hr only, same 404-not-403 shape
    used everywhere else for restricted records)."""
    result = get_required_skills_service(db, user, project_id, resolve_view_mode(user.role, view_mode))
    if result is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return result


@app.put("/projects/{project_id}/required-skills", response_model=list[ProjectSkillRequirementOut])
def set_required_skills_route(
    project_id: int,
    body: list[ProjectSkillRequirementIn],
    view_mode: str | None = Query(None, description='"work" or "employee" — see GET /people.'),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[ProjectSkillRequirementOut]:
    """Replaces the full required-skills set for this project. Only the
    project's owner or hr may call this — app/project_skills.py's own
    check, enforced there rather than only here."""
    try:
        result = set_required_skills_service(
            db, user, project_id, body, resolve_view_mode(user.role, view_mode))
    except ProjectNotWritable as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except UnknownSkill as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return result


def _require_continuity_access(user: AuthenticatedUser, view_mode: str | None) -> ViewMode:
    """The route-layer half of continuity's gate, in one place for all four
    endpoints — they had four copies of the same `if user.role != "hr"`, and
    the view_mode half needed adding to every one of them.

    Returns the resolved mode so the caller passes the *server's* decision
    down to the service, never the raw query parameter: resolve_view_mode
    pins an unrecognised value to the narrower lens rather than rejecting
    it, so a malformed `?view_mode=` can only ever close this view, never
    open it.
    """
    mode = resolve_view_mode(user.role, view_mode)
    if effective_role(user.role, mode) != "hr":
        raise HTTPException(status_code=403, detail="Continuity data is an HR-only view, in work mode")
    return mode


@app.get("/continuity/exposure", response_model=ContinuityOverview)
def continuity_exposure_route(
    window_days: int | None = Query(None, description="Lookahead window in days. Defaults to the configured value."),
    view_mode: str | None = Query(None, description='"work" or "employee" — see GET /people.'),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> ContinuityOverview:
    """Organization-wide continuity summary. HR in work mode only — see
    app/continuity.py's module docstring for why this route-level check
    duplicates the one app.continuity.get_org_exposure already does itself
    (same double-check pattern as /notifications/date-milestones and
    /people/{id}/training/{code} above)."""
    mode = _require_continuity_access(user, view_mode)
    return get_org_exposure_service(db, user, window_days=window_days, view_mode=mode)


@app.get("/continuity/engagement-exposure", response_model=list[EngagementExposure])
def continuity_engagement_exposure_route(
    exposure: str | None = None,
    client: str | None = None,
    project: str | None = None,
    office: str | None = None,
    org_unit: str | None = None,
    dependency_type: str | None = None,
    window_days: int | None = Query(None),
    view_mode: str | None = Query(None, description='"work" or "employee" — see GET /people.'),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[EngagementExposure]:
    """Filterable list of client engagements with continuity exposure.
    HR in work mode only."""
    mode = _require_continuity_access(user, view_mode)
    return get_engagement_exposure_service(
        db, user, exposure=exposure, client=client, project=project,
        office=office, org_unit=org_unit, dependency_type=dependency_type, window_days=window_days,
        view_mode=mode,
    )


@app.get("/continuity/review-queue", response_model=list[HrReviewQueueItem])
def continuity_review_queue_route(
    authorization_type: str | None = None,
    exposure: str | None = None,
    next_review_from: date | None = None,
    next_review_to: date | None = None,
    engagements_min: int | None = None,
    engagements_max: int | None = None,
    window_days: int | None = Query(None),
    view_mode: str | None = Query(None, description='"work" or "employee" — see GET /people.'),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[HrReviewQueueItem]:
    """The proactive "who is nearing a review date" list — every employee
    with a current, HR-verified record and a scheduled review, whether or
    not it has any client-engagement consequence. Complements
    /continuity/engagement-exposure, which only ever surfaces the subset
    whose review does intersect something. HR in work mode only."""
    mode = _require_continuity_access(user, view_mode)
    return get_hr_review_queue_service(
        db, user, window_days=window_days, authorization_type=authorization_type, exposure=exposure,
        next_review_from=next_review_from, next_review_to=next_review_to,
        engagements_min=engagements_min, engagements_max=engagements_max, view_mode=mode,
    )


@app.get("/continuity/employees/{employee_id}", response_model=EmployeeContinuityDetail)
def continuity_employee_route(
    employee_id: str,
    view_mode: str | None = Query(None, description='"work" or "employee" — see GET /people.'),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> EmployeeContinuityDetail:
    """HR drill-down: one employee's work-authorization history and their
    client-engagement exposure entries, including engagements where the
    review doesn't intersect (exposure="none") — unlike the org-wide list,
    nothing here is filtered out. HR in work mode only."""
    mode = _require_continuity_access(user, view_mode)
    result = get_employee_continuity_service(db, user, employee_id, view_mode=mode)
    if result is None:
        raise HTTPException(status_code=404, detail="Person not found")
    return result


@app.post(
    "/continuity/employees/{employee_id}/authorization-records", status_code=201,
    response_model=AuthorizationRecordOut,
)
def submit_authorization_record_route(
    employee_id: str,
    body: SubmitAuthorizationRecordRequest,
    view_mode: str | None = Query(None, description='"work" or "employee" — see GET /people.'),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> AuthorizationRecordOut:
    """Enter a new work-authorization record as pending_verification. It has
    no effect on continuity analysis or the HR review queue until POST
    .../confirm verifies it — see app/continuity.py's write-path section.
    HR in work mode only."""
    mode = _require_continuity_access(user, view_mode)
    try:
        return submit_authorization_record_service(
            db, user, employee_id, authorization_type=body.authorization_type,
            effective_from=body.effective_from, effective_until=body.effective_until,
            next_hr_review_date=body.next_hr_review_date,
            source_document_type=body.source_document_type, internal_notes=body.internal_notes,
            view_mode=mode,
        )
    except AuthorizationRecordNotFound as exc:
        raise HTTPException(status_code=404, detail="Person not found") from exc


@app.post(
    "/continuity/authorization-records/{record_id}/confirm", response_model=AuthorizationRecordOut,
)
def confirm_authorization_record_route(
    record_id: int,
    view_mode: str | None = Query(None, description='"work" or "employee" — see GET /people.'),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> AuthorizationRecordOut:
    """The verification gate: makes a pending record current, superseding
    whichever record was current before it. HR in work mode only."""
    mode = _require_continuity_access(user, view_mode)
    try:
        return confirm_authorization_record_service(db, user, record_id, view_mode=mode)
    except AuthorizationRecordNotFound as exc:
        raise HTTPException(status_code=404, detail="Authorization record not found") from exc
    except AuthorizationRecordNotActionable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post(
    "/continuity/authorization-records/{record_id}/reject", response_model=AuthorizationRecordOut,
)
def reject_authorization_record_route(
    record_id: int,
    view_mode: str | None = Query(None, description='"work" or "employee" — see GET /people.'),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> AuthorizationRecordOut:
    """Reject a pending submission. Kept, not deleted — see
    app.continuity.reject_authorization_record. HR in work mode only."""
    mode = _require_continuity_access(user, view_mode)
    try:
        return reject_authorization_record_service(db, user, record_id, view_mode=mode)
    except AuthorizationRecordNotFound as exc:
        raise HTTPException(status_code=404, detail="Authorization record not found") from exc
    except AuthorizationRecordNotActionable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post(
    "/continuity/review-queue/{record_id}/acknowledge", response_model=AuthorizationRecordOut,
)
def acknowledge_hr_review_route(
    record_id: int,
    view_mode: str | None = Query(None, description='"work" or "employee" — see GET /people.'),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> AuthorizationRecordOut:
    """Silence the upcoming-review reminder for this record's current due
    date. Lighter than POST .../confirm: it never changes
    next_hr_review_date, verification_status, or is_current, and the record
    keeps appearing in GET /continuity/review-queue regardless — this only
    stops app/notifications.py's sweep from nagging about it. HR in work
    mode only."""
    mode = _require_continuity_access(user, view_mode)
    try:
        return acknowledge_hr_review_service(db, user, record_id, view_mode=mode)
    except AuthorizationRecordNotFound as exc:
        raise HTTPException(status_code=404, detail="Authorization record not found") from exc
    except AuthorizationRecordNotActionable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Doc upload -> AI extraction -> IT review.
# ---------------------------------------------------------------------------

_UPLOAD_CHUNK_BYTES = 1024 * 1024


async def _read_upload_capped(file: UploadFile) -> bytes:
    """Read an UploadFile in chunks, aborting once config.max_upload_bytes()
    is exceeded.

    Reads via file.read() unbounded pull the whole body into memory before
    anything can check its size — for a request-triggered parse path
    (python-docx unzips the whole file, pypdf walks every page), an
    oversized or crafted upload turns straight into a memory/CPU hit in the
    request thread. Chunking keeps the checked total bounded regardless of
    what the client claims in Content-Length.
    """
    limit = config.max_upload_bytes()
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_UPLOAD_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise HTTPException(
                status_code=413,
                detail=f"File exceeds the {limit // (1024 * 1024)} MB upload limit",
            )
        chunks.append(chunk)
    return b"".join(chunks)


@app.post("/docs/upload", status_code=201)
async def upload_doc_route(
    file: UploadFile = File(..., description="A .docx or .pdf status document."),
    view_mode: str | None = Query(None, description='"work" or "employee" — see GET /people.'),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict:
    """Upload a document, parse it, and queue what it says for review.

    IT-only in work mode, same gate as the review endpoints — uploading is
    the first step of the review workflow, not a separate capability.

    Nothing here reaches EmployeeProject or EmployeeSkill: the response is a
    count of *pending* rows, every one of which needs an explicit accept.
    """
    mode = resolve_view_mode(user.role, view_mode)
    if user.role != "it" or mode != "work":
        raise HTTPException(
            status_code=403, detail="Uploading documents for extraction is an IT action in work mode")

    data = await _read_upload_capped(file)
    if not data:
        raise HTTPException(status_code=422, detail="Empty file")

    try:
        doc = store_document(db, user, file.filename or "", file.content_type, data)
    except UnsupportedDocument as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc

    doc_type, proposals = process_document(db, user, doc)
    people_mentioned = (
        db.query(DocSubjectMatch).filter(DocSubjectMatch.source_doc_id == doc.id).count()
    )
    return {
        "doc_id": doc.id,
        "filename": doc.filename,
        "characters_extracted": len(doc.extracted_text),
        "doc_type": doc_type.value,
        "people_mentioned": people_mentioned,
        "proposed_changes": len(proposals),
        "status": "pending",
    }


@app.get("/uploaded_docs")
def list_documents_route(
    view_mode: str | None = Query(None, description='"work" or "employee" — see GET /people.'),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict:
    """Every uploaded document, newest first — the review screen's index of
    what's awaiting a decision (pending_count, unresolved_subject_count)
    versus what's already been finalized (content_scrubbed_at set).

    Named /uploaded_docs, not the more obvious /docs — FastAPI's own
    interactive API documentation is already mounted at GET /docs
    (docs_url, the default), and a second route on the identical path
    would either shadow it or be shadowed by it depending on registration
    order. /docs/upload and /docs/{id}/finalize don't collide (FastAPI's
    reservation is the exact bare path), only a bare GET /docs would.
    """
    try:
        documents = list_documents(db, user, resolve_view_mode(user.role, view_mode))
    except ReviewDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return {"documents": documents}


@app.get("/doc_subject_matches")
def list_doc_subject_matches_route(
    doc_id: int | None = Query(None, description="Restrict to one uploaded document."),
    status: str | None = Query(None, description="unresolved | resolved | new_hire_candidate"),
    view_mode: str | None = Query(None, description='"work" or "employee" — see GET /people.'),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict:
    """The required first screen: every person a document mentions, with
    ranked candidate employees for each. Unresolved rows sort first."""
    try:
        subjects = list_subjects(
            db, user, resolve_view_mode(user.role, view_mode), doc_id=doc_id, status=status)
    except ReviewDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"unknown status: {status}") from exc
    return {"doc_id": doc_id, "subjects": subjects}


@app.post("/doc_subject_matches/{subject_id}/resolve")
def resolve_doc_subject_match_route(
    subject_id: int,
    body: ResolveSubjectRequest,
    view_mode: str | None = Query(None),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict:
    """Confirm who a mentioned person is (employee_id), or flag them for HR
    to create first (new_hire). Only after this does that person's
    proposed_changes rows become visible in GET /proposed_changes."""
    try:
        subject = resolve_subject(
            db, user, subject_id, resolve_view_mode(user.role, view_mode),
            employee_id=body.employee_id, new_hire=body.new_hire)
    except ReviewDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except SubjectNotFound as exc:
        raise HTTPException(status_code=404, detail="doc_subject_match not found") from exc
    except ProposalNotActionable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "id": subject.id,
        "extracted_name": subject.extracted_name,
        "resolution_status": subject.resolution_status.value,
        "resolved_employee_id": subject.resolved_employee_id,
        "resolved_by": subject.resolved_by,
        "resolved_at": subject.resolved_at,
    }


@app.get("/proposed_changes")
def list_proposed_changes_route(
    doc_id: int | None = Query(None, description="Restrict to one uploaded document."),
    employee_id: str | None = Query(None, description="Restrict to one employee."),
    status: str | None = Query(None, description="pending | accepted | edited | rejected"),
    view_mode: str | None = Query(None, description='"work" or "employee" — see GET /people.'),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict:
    """The review queue, grouped by employee. Only rows whose
    doc_subject_match has been resolved ever appear here — an unresolved
    person's proposals live in GET /doc_subject_matches instead."""
    try:
        groups = list_proposals(
            db, user, resolve_view_mode(user.role, view_mode),
            doc_id=doc_id, employee_id=employee_id, status=status)
    except ReviewDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"unknown status: {status}") from exc
    return {"doc_id": doc_id, "groups": groups}


@app.post("/proposed_changes/{proposal_id}/accept")
def accept_proposed_change_route(
    proposal_id: int,
    view_mode: str | None = Query(None),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict:
    """Commit one proposed change to the real tables, re-index, and audit it
    with source=ai_extraction. One of only two paths by which extracted
    content becomes searchable (the other is /edit)."""
    return _review_action(
        accept_proposal, db, user, proposal_id, resolve_view_mode(user.role, view_mode))


@app.post("/proposed_changes/{proposal_id}/edit")
def edit_proposed_change_route(
    proposal_id: int,
    body: EditProposalRequest,
    view_mode: str | None = Query(None),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict:
    """Commit the reviewer's own value — not the raw AI output. Status
    lands as `edited`, distinct from `accept`'s `accepted`, so extraction
    quality stays measurable."""
    return _review_action(
        edit_proposal, db, user, proposal_id, resolve_view_mode(user.role, view_mode),
        edited_value=body.edited_value)


@app.post("/proposed_changes/{proposal_id}/reassign")
def reassign_proposed_change_route(
    proposal_id: int,
    body: ReassignProposalRequest,
    view_mode: str | None = Query(None),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict:
    """Point one field-level proposal at a different employee, independent
    of its doc_subject_match. Stays pending."""
    return _review_action(
        reassign_proposal, db, user, proposal_id, resolve_view_mode(user.role, view_mode),
        employee_id=body.employee_id)


@app.post("/proposed_changes/{proposal_id}/correct")
def correct_proposed_change_route(
    proposal_id: int,
    body: CorrectProposalRequest,
    view_mode: str | None = Query(None),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict:
    """Send a correction back through the function-calling loop. Stays
    pending. An alternative to /edit for a harder case: ask the model to
    re-extract with a hint, rather than typing the corrected value directly."""
    return _review_action(
        correct_proposal, db, user, proposal_id, resolve_view_mode(user.role, view_mode),
        instruction=body.instruction)


@app.post("/proposed_changes/{proposal_id}/reject")
def reject_proposed_change_route(
    proposal_id: int,
    view_mode: str | None = Query(None),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict:
    """Reject a proposal. IT's fallback is the manual edit endpoints
    (PATCH /employees/{id}, PUT /projects/{id}/description)."""
    return _review_action(
        reject_proposal, db, user, proposal_id, resolve_view_mode(user.role, view_mode))


@app.post("/proposed_changes/{proposal_id}/undo")
def undo_proposed_change_route(
    proposal_id: int,
    view_mode: str | None = Query(None),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict:
    """Flip an accepted/edited proposal back to pending and reverse exactly
    what it wrote — only while its source document hasn't been finalized
    yet (see app.proposals.undo)."""
    return _review_action(
        undo_proposal, db, user, proposal_id, resolve_view_mode(user.role, view_mode))


@app.post("/proposed_changes/bulk_accept")
def bulk_accept_proposed_changes_route(
    body: BulkProposalRequest,
    view_mode: str | None = Query(None),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict:
    """Accept every matching pending row — a loop over the exact same
    accept() every single-row call goes through, no separate commit logic."""
    return _bulk_action(
        bulk_accept_proposals, db, user, resolve_view_mode(user.role, view_mode), body)


@app.post("/proposed_changes/bulk_reject")
def bulk_reject_proposed_changes_route(
    body: BulkProposalRequest,
    view_mode: str | None = Query(None),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict:
    return _bulk_action(
        bulk_reject_proposals, db, user, resolve_view_mode(user.role, view_mode), body)


@app.post("/docs/{doc_id}/finalize")
def finalize_document_route(
    doc_id: int,
    body: FinalizeDocumentRequest,
    view_mode: str | None = Query(None),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict:
    """The "Update" action: accept every id in accept_ids, reject every
    OTHER still-pending row this document has waiting, then clear the
    document's own extracted text for good — a one-shot pass, not
    repeatable once it's run once (see DocumentAlreadyFinalized)."""
    try:
        result = finalize_document_service(
            db, user, doc_id, resolve_view_mode(user.role, view_mode), accept_ids=body.accept_ids)
    except ReviewDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except DocumentNotFound as exc:
        raise HTTPException(status_code=404, detail="Document not found") from exc
    except DocumentAlreadyFinalized as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return result


def _bulk_action(fn, db, user, mode: str, body: BulkProposalRequest) -> dict:
    try:
        results = fn(db, user, mode, ids=body.ids, doc_id=body.doc_id, employee_id=body.employee_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"results": results}


def _review_action(fn, db, user, proposal_id: int, mode: str, **kwargs) -> dict:
    """Shared exception -> status-code translation for the single-row review
    actions. They differ only in which service function they call, and
    duplicating the same except-clauses for each one is how one of them
    ends up quietly returning 500 for a missing row."""
    try:
        proposal = fn(db, user, proposal_id, view_mode=mode, **kwargs)
    except ReviewDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ProposalNotFound as exc:
        raise HTTPException(status_code=404, detail="Proposed change not found") from exc
    except ProposalNotActionable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "id": proposal.id,
        "status": proposal.status.value,
        "employee_id": proposal.employee_id,
        "change_type": proposal.change_type.value,
        "proposed_value": json.loads(proposal.proposed_value),
        "original_value": json.loads(proposal.original_value) if proposal.original_value else None,
        "reviewed_by": proposal.reviewed_by,
        "reviewed_at": proposal.reviewed_at,
    }


# ---------------------------------------------------------------------------
# Community Graph — private per-employee "who to contact for what" list.
# See app/community_links.py's module docstring for the visibility rule
# (unconditionally owner-only, no role exception) and the mentor-link
# expiration mechanics.
# ---------------------------------------------------------------------------

@app.get("/community_links", response_model=list[CommunityLinkOut])
def list_community_links_route(
    view_mode: str | None = Query(None, description='"work" or "employee" — see GET /people.'),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[CommunityLinkOut]:
    """The caller's own community graph only — official + personal, merged,
    with mentor-link expiration applied. No person_id parameter, on
    purpose, same reason /me/notifications has none: there is no route
    shape here that could read someone else's graph, for any role.

    Links whose contact has been deactivated or restricted are omitted —
    view_mode is here because that visibility check is the same one
    GET /people/{id} applies, and the two have to agree or the client is
    handed a contact it cannot then look up."""
    mode = resolve_view_mode(user.role, view_mode)
    return list_community_links_service(db, user, mode)


@app.post("/community_links", status_code=201, response_model=CommunityLinkOut)
def create_community_link_route(
    body: CreateCommunityLinkRequest,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> CommunityLinkOut:
    """Add a personal link. owner is always the caller — the request body
    has no field for it, so nobody can add to someone else's graph even by
    supplying an id."""
    try:
        return create_personal_link_service(
            db, user, body.contact_employee_id, body.role_label, body.reason)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.patch("/community_links/{link_id}", response_model=CommunityLinkOut)
def update_community_link_route(
    link_id: int,
    body: UpdateCommunityLinkRequest,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> CommunityLinkOut:
    """Only if owner + source=personal, else 403 — official links are
    read-only regardless of role."""
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=422, detail="no fields supplied")
    try:
        return update_personal_link_service(db, user, link_id, changes)
    except LinkNotFound as exc:
        raise HTTPException(status_code=404, detail="Community link not found") from exc
    except LinkDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.delete("/community_links/{link_id}", status_code=204)
def delete_community_link_route(
    link_id: int,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> None:
    """Only if owner + source=personal, else 403 — same gate as PATCH."""
    try:
        delete_personal_link_service(db, user, link_id)
    except LinkNotFound as exc:
        raise HTTPException(status_code=404, detail="Community link not found") from exc
    except LinkDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.get("/suggested_official_links", response_model=list[SuggestedOfficialLinkOut])
def list_suggested_official_links_route(
    office_id: int | None = Query(None, description="Restrict to one office."),
    view_mode: str | None = Query(None, description='"work" or "employee" — see GET /people.'),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[SuggestedOfficialLinkOut]:
    """HR review queue for office/role -> candidate mappings bootstrapped
    from existing office/job-title data. HR-only."""
    try:
        return list_suggested_official_links_service(
            db, user, office_id=office_id, view_mode=resolve_view_mode(user.role, view_mode))
    except SuggestionDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.post("/suggested_official_links/generate", status_code=201, response_model=list[SuggestedOfficialLinkOut])
def generate_suggested_official_links_route(
    view_mode: str | None = Query(None, description='"work" or "employee" — see GET /people.'),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[SuggestedOfficialLinkOut]:
    """HR-triggered scan that stages new pending suggestions — never
    creates a real community_links edge itself. HR-only, same gate as the
    rest of the review queue."""
    if effective_role(user.role, resolve_view_mode(user.role, view_mode)) != "hr":
        raise HTTPException(
            status_code=403,
            detail="Generating official-link suggestions is an HR action, in work mode")
    return generate_suggested_official_links_service(db)


@app.post("/suggested_official_links/{suggestion_id}/confirm", response_model=SuggestedOfficialLinkOut)
def confirm_suggested_official_link_route(
    suggestion_id: int,
    view_mode: str | None = Query(None, description='"work" or "employee" — see GET /people.'),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> SuggestedOfficialLinkOut:
    """Creates the real official edge, fanned out to every active employee
    in the suggestion's office — see app.community_links for why."""
    try:
        return confirm_suggested_official_link_service(
            db, user, suggestion_id, resolve_view_mode(user.role, view_mode))
    except SuggestionDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except SuggestionNotFound as exc:
        raise HTTPException(status_code=404, detail="Suggestion not found") from exc
    except SuggestionNotActionable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/suggested_official_links/{suggestion_id}/reject", response_model=SuggestedOfficialLinkOut)
def reject_suggested_official_link_route(
    suggestion_id: int,
    view_mode: str | None = Query(None, description='"work" or "employee" — see GET /people.'),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> SuggestedOfficialLinkOut:
    try:
        return reject_suggested_official_link_service(
            db, user, suggestion_id, resolve_view_mode(user.role, view_mode))
    except SuggestionDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except SuggestionNotFound as exc:
        raise HTTPException(status_code=404, detail="Suggestion not found") from exc
    except SuggestionNotActionable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/community_links/auto_assign_mentors", status_code=201, response_model=list[CommunityLinkOut])
def auto_assign_mentors_route(
    view_mode: str | None = Query(None, description='"work" or "employee" — see GET /people.'),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[CommunityLinkOut]:
    """Sweep for new hires without a mentor and pair each with an eligible
    colleague, creating the official mentor link directly — see
    app.community_links.auto_assign_mentors for why this one official-link
    kind skips the suggest/confirm review queue the others go through.
    HR-only, same gate as the rest of the review queue."""
    try:
        return auto_assign_mentors_service(db, user, resolve_view_mode(user.role, view_mode))
    except SuggestionDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.post("/ask")
def ask(
    body: AskRequest,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict:
    """Natural-language entry point to the seven-function tool-calling
    layer. The model only ever emits a function name + arguments; every
    result here comes from the same permission-filtered service functions
    the structured endpoints above use — nothing bypasses the pipeline.

    body.history carries this browser session's prior turns as plans
    (tool + arguments), never results -- see schemas.HistoryTurn. Held by
    the client for follow-up chat; nothing here persists it server-side."""
    return answer_service(
        db, user, body.message, resolve_view_mode(user.role, body.view_mode), body.history)


# Built frontend (frontend/dist, produced by the CI/CD deploy job's frontend
# build step) is served from this same App Service -- one deploy target, one
# origin, so the frontend's fetch calls need no CORS or absolute API_BASE in
# production. Registered last so it never shadows an API route above; missing
# in local dev (nobody runs `vite build` there), hence the directory guard.
FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"

if FRONTEND_DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="frontend-assets")

    @app.get("/{full_path:path}")
    def serve_frontend(full_path: str) -> FileResponse:
        # Client-side routes (e.g. /profile/<id>) aren't real files -- fall
        # back to index.html and let the SPA's own router take it from there.
        candidate = FRONTEND_DIST / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(FRONTEND_DIST / "index.html")
