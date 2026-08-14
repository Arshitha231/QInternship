"""Rule 6: every write to an indexed field re-indexes.

Until now that rule was documented but unimplemented — the only way a
profile reached the search index was `python build_search_index.py`, a
manual full rebuild, so an edited bio or a newly-accepted project stayed
invisible to search until somebody remembered to run it. This module is the
hook the rule always implied, and build_search_index.py now shares its
document-building and upload code rather than owning a second copy.

Two properties matter more than throughput here:

1. **It never raises into its caller.** A write that succeeded in the
   database must not be reported as failed because Azure Search was
   briefly unreachable — the row is committed, the index is a derived
   cache, and the project's rule is "degradation, never errors". Failures
   are returned as a status, and the caller decides whether to care.

2. **It is a no-op when Search is unconfigured.** Tests blank the Search
   env vars (see tests/conftest.py) and local development usually has no
   Azure resources at all, so the common path is `is_configured() -> False`
   and an immediate return. Nothing about a write's behaviour changes based
   on whether an Azure resource happens to be reachable.

Synchronous on purpose. A background queue would be the right shape at real
volume, but there is no worker process in this deployment, and a write path
that silently drops re-index work when the process restarts would be worse
than one that takes an extra few hundred milliseconds and tells you when it
failed.
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx
from openai import OpenAI
from sqlalchemy.orm import Session

from app.models import Employee, EmployeeSkill, Office, OrgUnit, Skill
from app.search_client import (
    EMBEDDING_DEPLOYMENT,
    EMBEDDING_ENDPOINT,
    EMBEDDING_KEY,
    INDEX_NAME,
    SEARCH_API_VERSION,
    SEARCH_ENDPOINT,
    SEARCH_KEY,
    is_configured,
)

# Reuses search_client's module-level env reads rather than re-reading
# os.environ: one place decides what "configured" means, so the retrieval
# path and the write path can never disagree about which index they're
# pointed at. That disagreement is exactly the bug the seed script's
# docstring warns about (local ids published into the deployed index).


@dataclass
class ReindexResult:
    """What happened, for the caller's audit trail and nothing else."""

    attempted: bool  # False when Search isn't configured
    ok: bool
    detail: str | None = None


SKIPPED = ReindexResult(attempted=False, ok=True, detail="search not configured")


def _search_headers() -> dict:
    return {"api-key": SEARCH_KEY, "Content-Type": "application/json"}


def build_search_document(db: Session, employee: Employee) -> dict:
    """The indexed shape of one employee.

    Note what is NOT here: nothing HR-only, nothing ABAC-gated, and (via
    build_profile_text) nothing about confidential projects. See
    app/search_index.py — the corpus is built from the full record for every
    active employee precisely because the field list contains nothing that
    needs a caller to filter it.
    """
    from app.search_index import build_profile_text

    org_unit = db.get(OrgUnit, employee.org_unit_id) if employee.org_unit_id else None
    office = db.get(Office, employee.office_id) if employee.office_id else None
    skill_rows = (
        db.query(EmployeeSkill, Skill)
        .join(Skill, EmployeeSkill.skill_id == Skill.id)
        .filter(EmployeeSkill.employee_id == employee.id)
        .all()
    )
    skills = [
        {"name": sk.name, "level": es.level.value, "category": sk.category.value, "source": es.source.value}
        for es, sk in skill_rows
    ]
    return {
        "id": employee.id,
        "full_name": employee.full_name,
        "preferred_name": employee.preferred_name,
        "job_title": employee.job_title,
        "org_unit": org_unit.name if org_unit else None,
        "office": office.name if office else None,
        "office_city": office.city if office else None,
        "skills": skills,
        "availability_status": employee.availability_status.value,
        "is_active": employee.is_active,
        "profile_text": build_profile_text(db, employee),
    }


def _embedding_client() -> OpenAI | None:
    if not (EMBEDDING_ENDPOINT and EMBEDDING_KEY and EMBEDDING_DEPLOYMENT):
        return None
    return OpenAI(base_url=f"{EMBEDDING_ENDPOINT.rstrip('/')}/openai/v1/", api_key=EMBEDDING_KEY)


def upload_documents(docs: list[dict]) -> dict:
    url = f"{SEARCH_ENDPOINT}/indexes/{INDEX_NAME}/docs/index?api-version={SEARCH_API_VERSION}"
    payload = {"value": [{"@search.action": "mergeOrUpload", **doc} for doc in docs]}
    resp = httpx.post(url, headers=_search_headers(), json=payload, timeout=60.0)
    resp.raise_for_status()
    return resp.json()


def reindex_employee(db: Session, employee: Employee) -> ReindexResult:
    """Re-index one employee after a write to an indexed field.

    The embedding is best-effort *within* a best-effort operation: if the
    embedding call fails but Search is up, the document is still uploaded
    without profile_vector, so keyword and fuzzy retrieval stay correct and
    only semantic retrieval is stale. That's the same two-stage degradation
    app/search_client.py applies on the read side, in the same order.
    """
    if not is_configured():
        return SKIPPED

    try:
        doc = build_search_document(db, employee)
    except Exception as exc:  # a malformed row must not break the write
        return ReindexResult(attempted=True, ok=False, detail=f"document build failed: {exc}")

    client = _embedding_client()
    if client is not None:
        try:
            response = client.embeddings.create(model=EMBEDDING_DEPLOYMENT, input=[doc["profile_text"]])
            doc["profile_vector"] = response.data[0].embedding
        except Exception:
            # Deliberately broad: OpenAIError covers the API's own failures,
            # but a DNS/proxy/TLS problem surfaces as something else entirely
            # and must degrade the same way rather than propagating.
            pass  # upload without the vector rather than not at all

    try:
        result = upload_documents([doc])
    except Exception as exc:
        return ReindexResult(attempted=True, ok=False, detail=f"index upload failed: {exc}")

    statuses = result.get("value", [])
    if statuses and statuses[0].get("status") is not True:
        return ReindexResult(attempted=True, ok=False, detail=str(statuses[0].get("errorMessage")))
    return ReindexResult(attempted=True, ok=True)


def reindex_employee_id(db: Session, employee_id: str) -> ReindexResult:
    """Convenience wrapper for callers holding an id rather than a row —
    the proposed-change accept path, which resolves an employee_id long
    before it has a reason to load the employee itself."""
    employee = db.get(Employee, employee_id)
    if employee is None:
        return ReindexResult(attempted=False, ok=False, detail="employee not found")
    return reindex_employee(db, employee)
