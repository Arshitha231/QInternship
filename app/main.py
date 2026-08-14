from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Literal

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from app.auth import AuthenticatedUser, get_current_user
from app.db import engine, get_db
from app.org_chart import get_org_chain as get_org_chain_service
from app.people import find_people as find_people_service
from app.people import get_person as get_person_service
from app.people import update_own_bio as update_own_bio_service
from app.registry import assert_registry_covers_schema
from app.schemas import AskRequest, OrgChainNode, PersonDetail, PersonSummary, UpdateBioRequest
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
