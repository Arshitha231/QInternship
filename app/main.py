from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import AsyncIterator, Literal

from fastapi import Depends, FastAPI, HTTPException, Query
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
from app.models import Employee, TrainingCourse
from app.models.enums import CourseStatus, display_status
from app.notifications import notifications_for, notify_date_milestones
from app.org_chart import get_org_chain as get_org_chain_service
from app.people import find_people as find_people_service
from app.people import get_person as get_person_service
from app.people import update_own_bio as update_own_bio_service
from app.project_skills import ProjectNotWritable, UnknownSkill
from app.project_skills import get_required_skills as get_required_skills_service
from app.project_skills import set_required_skills as set_required_skills_service
from app.registry import assert_registry_covers_schema
from app.schemas import (
    AskRequest,
    ContinuityOverview,
    EmployeeContinuityDetail,
    EngagementExposure,
    HrReviewQueueItem,
    NotificationOut,
    OrgChainNode,
    PersonDetail,
    PersonRef,
    PersonSummary,
    ProjectSkillRequirementIn,
    ProjectSkillRequirementOut,
    RecordCourseStatusRequest,
    UpdateBioRequest,
)
from app.tool_calling import answer as answer_service
from app.unified_search import unified_search


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
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[PersonSummary]:
    return find_people_service(
        db, user, name=name, query=query, skill=skill, level=level, org_unit=org_unit,
        office=office, language=language, available=available,
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
    )
    result["results"] = [p.model_dump(exclude_unset=True) for p in result["results"]]
    if result.get("overview") is not None:
        result["overview"]["citations"] = [c.model_dump(exclude_unset=True) for c in result["overview"]["citations"]]
    return result


@app.get("/people/{person_id}", response_model=PersonDetail, response_model_exclude_unset=True)
def get_person_route(
    person_id: str,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
) -> PersonDetail:
    person = get_person_service(db, user, person_id)
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
    return get_hr_review_queue_service(db, user, window_days=window_days)


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
    return answer_service(db, user, body.message)


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
