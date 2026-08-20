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
from collections import OrderedDict

import httpx
from dotenv import load_dotenv
from openai import OpenAI, OpenAIError

from app.query_plan import Filter

load_dotenv()

SEARCH_ENDPOINT = os.environ.get("SEARCH_ENDPOINT", "").rstrip("/")
SEARCH_KEY = os.environ.get("SEARCH_KEY", "")
SEARCH_API_VERSION = "2024-07-01"
# ONE index exists for this project, and it belongs to the deployed app.
#
# The index stores employee ids, and ids are per-database: seed.py generates
# the same synthetic PEOPLE every run but fresh UUIDs, so a local SQLite
# dataset and the deployed Azure SQL one share names and share no ids
# (measured: 197/200 sampled index docs match a local employee by full_name,
# 5 by id). A local run against this index therefore resolves every ranked
# hit to nothing, which is why free-text search degrades to the SQL keyword
# path locally -- see app.people.find_people. That is the intended local
# behaviour under the one-index limit, not a bug to work around.
#
# Two workarounds were considered and rejected. Rebuilding this index from
# local data breaks search for the deployed app and everyone using it (the
# collision seed.py's own docstring warns about). Resolving ranked hits by
# full_name instead of id would make one index serve both, and would also
# make search silently return the WRONG person whenever two people share a
# name -- the dataset seeds two "Priya Sharma"s precisely because that case
# has to stay unresolvable rather than become a coin flip.
#
# The env var stays because it costs nothing and is the honest fix the day a
# second index is available. Default unchanged, so deployment needs no config.
INDEX_NAME = os.environ.get("SEARCH_INDEX_NAME", "employees-index")

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


def _apply_required_filter(clauses: list[str], f: Filter) -> None:
    """Translates one app.policy.PolicyDecision obligation into an OData
    clause, appended to `clauses` in place -- the Search-side counterpart to
    app.query_compiler.apply_filter, so the restricted-employee rule is
    excluded at the index-query level instead of being fetched and then
    filtered out in Python after the fact.

    Raises on any obligation shape it doesn't recognize, deliberately, rather
    than silently doing nothing -- today enforce() only ever produces one
    obligation (availability_status ne restricted); a second obligation type
    added later must be taught here explicitly, or it would silently never
    reach the Search path while still being enforced correctly on the SQL
    path -- exactly the kind of divergence the RC3 truncation bug came from.
    """
    if f.field == "availability_status" and f.op == "ne" and isinstance(f.value, str):
        clauses.append(f"availability_status ne '{_escape_odata(f.value)}'")
        return
    raise ValueError(f"_build_filter does not know how to translate obligation {f!r} into an OData clause")


def _build_filter(
    *, org_unit: str | list[str] | None, skill: str | None, level: str | None,
    office: str | None, language: str | None, available: bool | None,
    required_filters: list[Filter] | None = None,
) -> str:
    clauses = ["is_active eq true"]  # data hygiene, not a permission decision
    for f in required_filters or []:
        _apply_required_filter(clauses, f)
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
    vectors = embed_texts([text])
    return None if vectors is None else vectors[0]


# Bounded and in-process. Sized to hold the whole project corpus (128 rows)
# plus a working set of query and sentence strings, so a steady-state
# semantic search embeds only the one string it has genuinely not seen.
_VECTOR_CACHE: "OrderedDict[str, list[float]]" = OrderedDict()
_VECTOR_CACHE_MAX = 2048


def _cache_vector(text: str, vector: list[float]) -> None:
    _VECTOR_CACHE[text] = vector
    _VECTOR_CACHE.move_to_end(text)
    while len(_VECTOR_CACHE) > _VECTOR_CACHE_MAX:
        _VECTOR_CACHE.popitem(last=False)


def clear_vector_cache() -> None:
    """Test hook, mirroring app.tool_calling.clear_response_cache."""
    _VECTOR_CACHE.clear()


# Batch ceiling per embeddings request. The whole project corpus (128 rows)
# fits well inside one call, but chunking keeps a future larger corpus from
# hitting the endpoint's own per-request input limit.
EMBED_BATCH_SIZE = 96


def embed_texts(texts: list[str]) -> list[list[float]] | None:
    """Embed a batch in as few calls as possible, or None if the embedding
    resource is unconfigured/unreachable.

    None rather than an exception, and all-or-nothing rather than partial:
    every caller here (hybrid search's vector arm, app/project_search.py's
    corpus build) degrades to a non-vector path on None, and a partially
    embedded corpus would rank some projects against a query and silently
    omit others — worse than not ranking semantically at all.

    Results stay in the order the texts were given, which is what lets
    project_search.rebuild_embeddings() zip them back onto its pending rows.
    """
    if not texts:
        return []

    # Only the texts we haven't embedded before are sent. An embedding is a
    # pure function of its input and carries no caller identity, so there is
    # nothing here that can cross a permission boundary -- the vectors only
    # ever RANK rows that are permission-filtered afterwards.
    #
    # This is worth more than a query cache, because the expensive caller
    # isn't the query. app/project_search.py's excerpt picker embeds the
    # problem PLUS every sentence of every matched project, on every single
    # request -- index-time work being redone at query time. Those sentences
    # come from project descriptions and barely ever change, so after the
    # first request the batch collapses to the one new problem string.
    # Measured: ~2.0s -> ~0.2s on the semantic path.
    missing = [t for t in dict.fromkeys(texts) if t not in _VECTOR_CACHE]
    if missing:
        client = _get_openai_client()
        if client is None:
            return None
        try:
            for start in range(0, len(missing), EMBED_BATCH_SIZE):
                chunk = missing[start:start + EMBED_BATCH_SIZE]
                response = client.embeddings.create(model=EMBEDDING_DEPLOYMENT, input=chunk)
                # The API documents `data` as returned in input order, but it
                # also carries an explicit index per item -- sorting on it costs
                # nothing and removes the assumption entirely.
                for text, item in zip(chunk, sorted(response.data, key=lambda d: d.index)):
                    _cache_vector(text, item.embedding)
        except OpenAIError:
            # All-or-nothing, as documented above: a partially embedded
            # corpus ranks some rows and silently drops others. Anything
            # already cached from earlier calls stays -- it is still valid.
            return None
    return [_VECTOR_CACHE[t] for t in texts]


def search_people(
    *,
    name: str | None = None,
    skill: str | None = None,
    level: str | None = None,
    org_unit: str | list[str] | None = None,
    office: str | None = None,
    language: str | None = None,
    available: bool | None = None,
    required_filters: list[Filter] | None = None,
    top: int,
) -> list[str] | None:
    """Ranked employee IDs for a free-text query and/or structured filters,
    or None if Search itself is unavailable/unconfigured/erroring — the
    caller falls back to SQL. `name=None` is a filter-only call (no text to
    rank on): submits `search: "*"` with just the OData filter applied, and
    skips the vector query entirely (nothing to embed).

    `required_filters` carries app.policy.PolicyDecision obligations (the
    restricted-employee rule, today) straight into the OData filter, so
    excluded rows never come back from the index at all -- this module still
    never sees the caller's role or makes its own visibility decision; it
    only compiles whatever obligation the caller already decided into Search's
    own filter syntax, same division of responsibility as before, just one
    more filter source alongside the structured search args.
    """
    if not is_configured():
        return None

    body: dict = {
        "search": _name_query(name) if name else "*",
        "queryType": "full",
        "filter": _build_filter(org_unit=org_unit, skill=skill, level=level,
                                office=office, language=language, available=available,
                                required_filters=required_filters),
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
