"""Azure AI Search retrieval for find_people — hybrid RRF (keyword+prefix,
fuzzy, and vector, combined natively by Azure when a text query and a
vector query are submitted together), replacing the SQL query as the
*retrieval* step only.

This module returns ranked employee IDs and nothing else. It never sees
the caller's role, never applies is_record_visible or any other permission
rule, and never builds a response. Everything downstream — record
filtering, field filtering, capping, audit — is the same unchanged Python
in app/people.py regardless of whether the IDs came from here or from SQL.
Retrieve moved to Search; filtering did not move anywhere.

Degrades in two independent stages, matching "degradation, never errors":
  - Azure OpenAI unavailable/unconfigured/erroring at query time -> drop
    the vector query, submit keyword+fuzzy only (still real Search).
  - Azure AI Search unavailable/unconfigured/erroring -> return None,
    telling find_people() to fall back to the plain SQL path entirely.
"""
from __future__ import annotations

import os
import re

import httpx
from dotenv import load_dotenv
from openai import OpenAI, OpenAIError

load_dotenv()

SEARCH_ENDPOINT = os.environ.get("SEARCH_ENDPOINT", "").rstrip("/")
SEARCH_KEY = os.environ.get("SEARCH_KEY", "")
SEARCH_API_VERSION = "2024-07-01"
INDEX_NAME = "employees-index"

# Its own resource, separate from chat (app/tool_calling.py) — a v1-API
# Azure AI Foundry endpoint, not a classic per-resource Azure OpenAI
# deployment, so this uses the plain OpenAI SDK client pointed at
# {endpoint}/openai/v1/ rather than AzureOpenAI's azure_endpoint+
# api_version+deployments/{name} URL shape. Confirmed live: this resource
# accepts the model catalog id directly as `model` with no separate
# per-account deployment name.
EMBEDDING_ENDPOINT = os.environ.get("EMBEDDING_ENDPOINT", "")
EMBEDDING_KEY = os.environ.get("EMBEDDING_KEY", "")
EMBEDDING_DEPLOYMENT = os.environ.get("OPENAI_EMBEDDING_DEPLOYMENT", "")

_LUCENE_SPECIAL = re.compile(r'([+\-!(){}\[\]^"~*?:\\/&|])')


def is_configured() -> bool:
    return bool(SEARCH_ENDPOINT and SEARCH_KEY)


def _escape_lucene(token: str) -> str:
    return _LUCENE_SPECIAL.sub(r"\\\1", token)


def _escape_odata(value: str) -> str:
    return value.replace("'", "''")


def _name_query(name: str) -> str:
    """Keyword+prefix OR'd with fuzzy — the two distinct techniques the
    spec calls for (exact/partial name vs. misspelled name), in one query
    string, submitted alongside the vector query so all three input types
    (name, misspelled name, description) go through one Search call."""
    tokens = [t for t in name.strip().split() if t]
    if not tokens:
        return "*"
    escaped = [_escape_lucene(t) for t in tokens]
    prefix_clause = " ".join(f"{t}*" for t in escaped)
    fuzzy_clause = " ".join(f"{t}~1" for t in escaped)
    return f"({prefix_clause}) OR ({fuzzy_clause})"


def _build_filter(
    *, org_unit: str | list[str] | None, skill: str | None, level: str | None,
    office: str | None, language: str | None, available: bool | None,
) -> str:
    clauses = ["is_active eq true"]  # data hygiene, not a permission decision
    if org_unit:
        # A hierarchy-expanded filter (a department name resolves to every
        # descendant team) arrives as a list of leaf names — the index only
        # ever stores an employee's single most-specific unit, so a
        # department-level filter has to OR across all of them. The common
        # single-unit case stays a plain equality clause.
        names = org_unit if isinstance(org_unit, list) else [org_unit]
        if len(names) == 1:
            clauses.append(f"org_unit eq '{_escape_odata(names[0])}'")
        else:
            or_clause = " or ".join(f"org_unit eq '{_escape_odata(n)}'" for n in names)
            clauses.append(f"({or_clause})")
    if office:
        esc = _escape_odata(office)
        clauses.append(f"(office eq '{esc}' or office_city eq '{esc}')")
    if available:
        # Same restriction as the SQL path: only the positive filter is
        # exposed, since "who's away" in aggregate is what's restricted.
        clauses.append("availability_status eq 'available'")
    if skill:
        skill_clause = f"s/name eq '{_escape_odata(skill)}'"
        if level:
            skill_clause += f" and s/level eq '{_escape_odata(level)}'"
        clauses.append(f"skills/any(s: {skill_clause})")
    if language:
        clauses.append(f"skills/any(s: s/category eq 'language' and s/name eq '{_escape_odata(language)}')")
    return " and ".join(clauses)


_openai_client: OpenAI | None = None


def _get_openai_client() -> OpenAI | None:
    global _openai_client
    if not (EMBEDDING_ENDPOINT and EMBEDDING_KEY and EMBEDDING_DEPLOYMENT):
        return None
    if _openai_client is None:
        _openai_client = OpenAI(base_url=f"{EMBEDDING_ENDPOINT.rstrip('/')}/openai/v1/", api_key=EMBEDDING_KEY)
    return _openai_client


def _embed_query(text: str) -> list[float] | None:
    """Query embedding per request (index-time embedding was the batch job
    in step 7). Any failure here just drops the vector half of the hybrid
    search — never propagates as an error."""
    client = _get_openai_client()
    if client is None:
        return None
    try:
        response = client.embeddings.create(model=EMBEDDING_DEPLOYMENT, input=[text])
        return response.data[0].embedding
    except OpenAIError:
        return None


def search_people(
    *,
    name: str | None = None,
    skill: str | None = None,
    level: str | None = None,
    org_unit: str | list[str] | None = None,
    office: str | None = None,
    language: str | None = None,
    available: bool | None = None,
    top: int,
) -> list[str] | None:
    """Ranked employee IDs for a free-text query and/or structured filters,
    or None if Search itself is unavailable/unconfigured/erroring — the
    caller falls back to SQL. `name=None` is a filter-only call (no text to
    rank on): submits `search: "*"` with just the OData filter applied, and
    skips the vector query entirely (nothing to embed).
    """
    if not is_configured():
        return None

    body: dict = {
        "search": _name_query(name) if name else "*",
        "queryType": "full",
        "filter": _build_filter(org_unit=org_unit, skill=skill, level=level,
                                office=office, language=language, available=available),
        "select": "id",
        "top": top,
    }
    vector = _embed_query(name) if name else None
    if vector is not None:
        body["vectorQueries"] = [{"kind": "vector", "vector": vector, "fields": "profile_vector", "k": top}]

    url = f"{SEARCH_ENDPOINT}/indexes/{INDEX_NAME}/docs/search?api-version={SEARCH_API_VERSION}"
    try:
        resp = httpx.post(url, headers={"api-key": SEARCH_KEY, "Content-Type": "application/json"},
                          json=body, timeout=10.0)
        resp.raise_for_status()
    except httpx.HTTPError:
        return None  # Search unavailable/erroring -> full SQL fallback

    return [r["id"] for r in resp.json().get("value", [])]
