from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query
from sqlalchemy.orm import Session

from app.auth import AuthenticatedUser, get_current_user
from app.db import engine, get_db
from app.org_chart import get_org_chain as get_org_chain_service
from app.people import find_people as find_people_service
from app.people import get_person as get_person_service
from app.schemas import OrgChainNode, PersonDetail, PersonSummary

app = FastAPI(
    title="Employee Directory API",
    description="Internal employee directory with permission-filtered natural-language search.",
    version="0.1.0",
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


@app.get("/people", response_model=list[PersonSummary])
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
