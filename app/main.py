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

from app.auth import AuthenticatedUser, get_current_user
from app.certifications import LocalStatusWritesDisabled, UnknownCourse, record_course_status
from app.continuity import get_employee_continuity as get_employee_continuity_service
from app.continuity import get_engagement_exposure as get_engagement_exposure_service
from app.continuity import get_hr_review_queue as get_hr_review_queue_service
from app.continuity import get_org_exposure as get_org_exposure_service
from app.db import engine, get_db
from app.doc_extraction import UnsupportedDocument, process_document, store_document
from app.models import DocSubjectMatch, Employee, TrainingCourse
from app.models.enums import CourseStatus, display_status
from app.notifications import notifications_for, notify_date_milestones
from app.org_chart import get_org_chain as get_org_chain_service
from app.people import find_people as find_people_service
from app.people import get_person as get_person_service
from app.people import update_own_bio as update_own_bio_service
from app.permissions import resolve_view_mode
from app.project_skills import ProjectNotWritable, UnknownSkill
from app.project_skills import get_required_skills as get_required_skills_service
from app.project_skills import set_required_skills as set_required_skills_service
from app.proposals import ProposalNotActionable, ProposalNotFound, ReviewDenied, SubjectNotFound
from app.proposals import accept as accept_proposal
from app.proposals import bulk_accept as bulk_accept_proposals
from app.proposals import bulk_reject as bulk_reject_proposals
from app.proposals import correct as correct_proposal
from app.proposals import edit as edit_proposal
from app.proposals import list_proposals
from app.proposals import list_subjects
from app.proposals import reassign as reassign_proposal
from app.proposals import reject as reject_proposal
from app.proposals import resolve_subject
from app.registry import assert_registry_covers_schema
from app.schemas import (
    AskRequest,
    BulkProposalRequest,
    ContinuityOverview,
    CorrectProposalRequest,
    EditProposalRequest,
    EmployeeContinuityDetail,
    EngagementExposure,
    HrReviewQueueItem,
    NotificationOut,
    OrgChainNode,
    PersonDetail,
    PersonRef,
    PersonSummary,
    ProjectDescriptionRequest,
    ProjectSkillRequirementIn,
    ProjectSkillRequirementOut,
    ReassignProposalRequest,
    RecordCourseStatusRequest,
    ResolveSubjectRequest,
    UpdateBioRequest,
    UpdateEmployeeRequest,
)
from app.tool_calling import answer as answer_service
from app.unified_search import unified_search
from app.writes import WriteDenied, WriteTargetMissing
from app.writes import clear_project_description as clear_project_description_service
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
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[OrgChainNode]:
    result = get_org_chain_service(db, user, person_id, direction, depth)
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
    if user.role != "hr":
        raise HTTPException(status_code=403, detail="Recording course status is an HR action")

    employee = db.get(Employee, person_id)
    if employee is None or not employee.is_active:
        raise HTTPException(status_code=404, detail="Person not found")

    try:
        row, notifications = record_course_status(
            db, employee=employee, course_code=course_code,
            status=CourseStatus(body.status),
            attempted_on=body.attempted_on, completed_on=body.completed_on,
        )
    except LocalStatusWritesDisabled as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except UnknownCourse as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

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
    if user.role != "hr":
        raise HTTPException(status_code=403, detail="Running the date sweep is an HR action")

    on_date = on or date.today()
    created = notify_date_milestones(db, on_date)
    db.commit()

    by_kind: dict[str, int] = {}
    for n in created:
        by_kind[n.kind.value] = by_kind.get(n.kind.value, 0) + 1
    return {
        "date": on_date.isoformat(),
        "notifications_sent": len(created),
        "by_kind": by_kind,
    }


@app.get("/projects/{project_id}/required-skills", response_model=list[ProjectSkillRequirementOut])
def get_required_skills_route(
    project_id: int,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[ProjectSkillRequirementOut]:
    """What a project's delivery actually needs, per app/project_skills.py.
    Not sensitive — visible to anyone who can see the project at all
    (confidential projects: members and hr only, same 404-not-403 shape
    used everywhere else for restricted records)."""
    result = get_required_skills_service(db, user, project_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return result


@app.put("/projects/{project_id}/required-skills", response_model=list[ProjectSkillRequirementOut])
def set_required_skills_route(
    project_id: int,
    body: list[ProjectSkillRequirementIn],
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[ProjectSkillRequirementOut]:
    """Replaces the full required-skills set for this project. Only the
    project's owner or hr may call this — app/project_skills.py's own
    check, enforced there rather than only here."""
    try:
        result = set_required_skills_service(db, user, project_id, body)
    except ProjectNotWritable as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except UnknownSkill as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return result


@app.get("/continuity/exposure", response_model=ContinuityOverview)
def continuity_exposure_route(
    window_days: int | None = Query(None, description="Lookahead window in days. Defaults to the configured value."),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> ContinuityOverview:
    """Organization-wide continuity summary. HR-only — see app/continuity.py's
    module docstring for why this route-level check duplicates the one
    app.continuity.get_org_exposure already does itself (same double-check
    pattern as /notifications/date-milestones and /people/{id}/training/{code}
    above)."""
    if user.role != "hr":
        raise HTTPException(status_code=403, detail="Continuity data is an HR-only view")
    return get_org_exposure_service(db, user, window_days=window_days)


@app.get("/continuity/engagement-exposure", response_model=list[EngagementExposure])
def continuity_engagement_exposure_route(
    exposure: str | None = None,
    client: str | None = None,
    project: str | None = None,
    office: str | None = None,
    org_unit: str | None = None,
    dependency_type: str | None = None,
    window_days: int | None = Query(None),
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[EngagementExposure]:
    """Filterable list of client engagements with continuity exposure.
    HR-only."""
    if user.role != "hr":
        raise HTTPException(status_code=403, detail="Continuity data is an HR-only view")
    return get_engagement_exposure_service(
        db, user, exposure=exposure, client=client, project=project,
        office=office, org_unit=org_unit, dependency_type=dependency_type, window_days=window_days,
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
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[HrReviewQueueItem]:
    """The proactive "who is nearing a review date" list — every employee
    with a current, HR-verified record and a scheduled review, whether or
    not it has any client-engagement consequence. Complements
    /continuity/engagement-exposure, which only ever surfaces the subset
    whose review does intersect something. HR-only."""
    if user.role != "hr":
        raise HTTPException(status_code=403, detail="Continuity data is an HR-only view")
    return get_hr_review_queue_service(
        db, user, window_days=window_days, authorization_type=authorization_type, exposure=exposure,
        next_review_from=next_review_from, next_review_to=next_review_to,
        engagements_min=engagements_min, engagements_max=engagements_max,
    )


@app.get("/continuity/employees/{employee_id}", response_model=EmployeeContinuityDetail)
def continuity_employee_route(
    employee_id: str,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> EmployeeContinuityDetail:
    """HR drill-down: one employee's work-authorization history and their
    client-engagement exposure entries, including engagements where the
    review doesn't intersect (exposure="none") — unlike the org-wide list,
    nothing here is filtered out. HR-only."""
    if user.role != "hr":
        raise HTTPException(status_code=403, detail="Continuity data is an HR-only view")
    result = get_employee_continuity_service(db, user, employee_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Person not found")
    return result


# ---------------------------------------------------------------------------
# Doc upload -> AI extraction -> IT review.
# ---------------------------------------------------------------------------

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

    data = await file.read()
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


@app.post("/ask")
def ask(
    body: AskRequest,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict:
    """Natural-language entry point to the seven-function tool-calling
    layer. The model only ever emits a function name + arguments; every
    result here comes from the same permission-filtered service functions
    the structured endpoints above use — nothing bypasses the pipeline."""
    return answer_service(db, user, body.message, resolve_view_mode(user.role, body.view_mode))


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
