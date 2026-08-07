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
from openai import AzureOpenAI, OpenAIError

load_dotenv()

SEARCH_ENDPOINT = os.environ.get("SEARCH_ENDPOINT", "").rstrip("/")
SEARCH_KEY = os.environ.get("SEARCH_KEY", "")
SEARCH_API_VERSION = "2024-07-01"
INDEX_NAME = "employees-index"

OPENAI_ENDPOINT = os.environ.get("OPENAI_ENDPOINT", "")
OPENAI_KEY = os.environ.get("OPENAI_KEY", "")
OPENAI_EMBEDDING_DEPLOYMENT = os.environ.get("OPENAI_EMBEDDING_DEPLOYMENT", "")
OPENAI_API_VERSION = "2024-06-01"

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
    *, org_unit: str | None, skill: str | None, level: str | None,
    office: str | None, language: str | None, available: bool | None,
) -> str:
    clauses = ["is_active eq true"]  # data hygiene, not a permission decision
    if org_unit:
        clauses.append(f"org_unit eq '{_escape_odata(org_unit)}'")
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


_openai_client: AzureOpenAI | None = None


def _get_openai_client() -> AzureOpenAI | None:
    global _openai_client
    if not (OPENAI_ENDPOINT and OPENAI_KEY and OPENAI_EMBEDDING_DEPLOYMENT):
        return None
    if _openai_client is None:
        _openai_client = AzureOpenAI(azure_endpoint=OPENAI_ENDPOINT, api_key=OPENAI_KEY, api_version=OPENAI_API_VERSION)
    return _openai_client


def _embed_query(text: str) -> list[float] | None:
    """Query embedding per request (index-time embedding was the batch job
    in step 7). Any failure here just drops the vector half of the hybrid
    search — never propagates as an error."""
    client = _get_openai_client()
    if client is None:
        return None
    try:
        response = client.embeddings.create(model=OPENAI_EMBEDDING_DEPLOYMENT, input=[text])
        return response.data[0].embedding
    except OpenAIError:
        return None


def search_people(
    *,
    name: str,
    skill: str | None = None,
    level: str | None = None,
    org_unit: str | None = None,
    office: str | None = None,
    language: str | None = None,
    available: bool | None = None,
    top: int,
) -> list[str] | None:
    """Ranked employee IDs for a free-text query, or None if Search itself
    is unavailable/unconfigured/erroring — the caller falls back to SQL.
    Only meaningful when there's a `name` query to rank on; pure-filter
    browsing with no free text stays on the SQL path (find_people never
    calls this without a name).
    """
    if not is_configured():
        return None

    body: dict = {
        "search": _name_query(name),
        "queryType": "full",
        "filter": _build_filter(org_unit=org_unit, skill=skill, level=level,
                                office=office, language=language, available=available),
        "select": "id",
        "top": top,
    }
    vector = _embed_query(name)
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
