"""Step 7 (the Azure-connected part): create the Azure AI Search index from
search_index_schema.json, then batch-embed and upload all active seed
employees using build_profile_text() + the Azure OpenAI embedding
deployment.

Unlike everything else built so far, this actually calls Azure — it needs
SEARCH_ENDPOINT, SEARCH_KEY, EMBEDDING_ENDPOINT, EMBEDDING_KEY, and
OPENAI_EMBEDDING_DEPLOYMENT in .env. Never errors out mid-batch: a failure
on one employee (embedding call or index upload) is recorded and the rest
continue, matching the project's "degradation, never errors" principle.
"""
from __future__ import annotations

import json
import os
import sys
import time

import httpx
from dotenv import load_dotenv
from openai import OpenAI, OpenAIError
from sqlalchemy import select

load_dotenv()

from app.db import SessionLocal  # noqa: E402
from app.models import Employee  # noqa: E402
from app.search_index import INDEX_SCHEMA  # noqa: E402
from app.search_reindex import build_search_document, upload_documents  # noqa: E402

REQUIRED_ENV = ["SEARCH_ENDPOINT", "SEARCH_KEY", "EMBEDDING_ENDPOINT", "EMBEDDING_KEY", "OPENAI_EMBEDDING_DEPLOYMENT"]
missing = [v for v in REQUIRED_ENV if not os.environ.get(v)]
if missing:
    sys.exit(f"Missing required .env values: {', '.join(missing)}")

SEARCH_ENDPOINT = os.environ["SEARCH_ENDPOINT"].rstrip("/")
SEARCH_KEY = os.environ["SEARCH_KEY"]
SEARCH_API_VERSION = "2024-07-01"
# Honours SEARCH_INDEX_NAME too, so "build an index for my local data"
# cannot silently overwrite the shared one -- see app/search_client.py.
INDEX_NAME = os.environ.get("SEARCH_INDEX_NAME", INDEX_SCHEMA["name"])

# Its own resource, separate from chat — see app/search_client.py's comment
# for why this is the plain OpenAI client (base_url=.../openai/v1/) rather
# than AzureOpenAI, and why the model catalog id is passed directly with no
# separate deployment name.
EMBEDDING_ENDPOINT = os.environ["EMBEDDING_ENDPOINT"]
EMBEDDING_KEY = os.environ["EMBEDDING_KEY"]
EMBEDDING_DEPLOYMENT = os.environ["OPENAI_EMBEDDING_DEPLOYMENT"]

BATCH_SIZE = 20
BATCH_PAUSE_SECONDS = 0.6  # stays comfortably under the deployment's 20-req/10s rate limit
MAX_RETRIES = 3


def _search_headers() -> dict:
    return {"api-key": SEARCH_KEY, "Content-Type": "application/json"}


def _with_retries(fn, *args, **kwargs):
    last_exc = None
    for attempt in range(MAX_RETRIES):
        try:
            return fn(*args, **kwargs)
        except (httpx.HTTPStatusError, OpenAIError) as exc:
            last_exc = exc
            status = getattr(getattr(exc, "response", None), "status_code", None) or getattr(exc, "status_code", None)
            if status == 429 or status is None:
                time.sleep(2 ** attempt)
                continue
            raise
    raise last_exc


def create_index() -> None:
    url = f"{SEARCH_ENDPOINT}/indexes/{INDEX_NAME}?api-version={SEARCH_API_VERSION}"
    resp = httpx.put(url, headers=_search_headers(), json=INDEX_SCHEMA, timeout=30.0)
    resp.raise_for_status()


def embed_batch(client: OpenAI, texts: list[str]) -> list[list[float]]:
    response = _with_retries(client.embeddings.create, model=EMBEDDING_DEPLOYMENT, input=texts)
    return [d.embedding for d in response.data]


def upload_batch(docs: list[dict]) -> dict:
    """The shared single-request upload, wrapped in this script's retry
    policy. The hook in app/search_reindex.py deliberately does not retry —
    it runs inside a request and degrades instead of blocking — whereas a
    bulk rebuild is a long offline job where backing off is the right
    behaviour."""
    return _with_retries(upload_documents, docs)


def fetch_document(doc_id: str) -> dict:
    url = f"{SEARCH_ENDPOINT}/indexes/{INDEX_NAME}/docs('{doc_id}')?api-version={SEARCH_API_VERSION}"
    resp = httpx.get(url, headers=_search_headers(), timeout=30.0)
    resp.raise_for_status()
    return resp.json()


def main() -> None:
    print(f"Creating/updating index '{INDEX_NAME}' at {SEARCH_ENDPOINT} ...")
    create_index()
    print("Index ready.\n")

    openai_client = OpenAI(base_url=f"{EMBEDDING_ENDPOINT.rstrip('/')}/openai/v1/", api_key=EMBEDDING_KEY)

    db = SessionLocal()
    indexed = 0
    failures: list[tuple[str, str, str]] = []
    sample_doc_id: str | None = None

    try:
        # `.is_(True)` renders as `IS 1`, which T-SQL rejects outright ("Incorrect
        # syntax near '1'") -- `== True` renders as `= 1` and works on every
        # backend. app/people.py, app/org_chart.py and app/directory_tools.py all
        # carry this same note; this script escaped the fix because it had only
        # ever been run locally against SQLite, where `IS 1` is perfectly legal.
        # It surfaced the first time anyone rebuilt the index from the App
        # Service, which is the only place it can be run correctly.
        employees = db.execute(
            select(Employee).where(Employee.is_active == True)  # noqa: E712
        ).scalars().all()
        total = len(employees)
        print(f"Embedding + indexing {total} employees (batches of {BATCH_SIZE}) ...")

        for i in range(0, total, BATCH_SIZE):
            batch = employees[i:i + BATCH_SIZE]
            docs = [build_search_document(db, e) for e in batch]
            texts = [d["profile_text"] for d in docs]

            try:
                vectors = embed_batch(openai_client, texts)
            except Exception as exc:
                for e in batch:
                    failures.append((e.id, e.full_name, f"embedding failed: {exc}"))
                print(f"  [{min(i + BATCH_SIZE, total)}/{total}] embedding call FAILED for this batch: {exc}")
                continue

            for doc, vector in zip(docs, vectors):
                doc["profile_vector"] = vector

            try:
                result = upload_batch(docs)
            except Exception as exc:
                for e in batch:
                    failures.append((e.id, e.full_name, f"index upload failed: {exc}"))
                print(f"  [{min(i + BATCH_SIZE, total)}/{total}] index upload FAILED for this batch: {exc}")
                continue

            for doc, status in zip(docs, result.get("value", [])):
                if status.get("status") is True:
                    indexed += 1
                    if sample_doc_id is None:
                        sample_doc_id = doc["id"]
                else:
                    failures.append((doc["id"], doc["full_name"], status.get("errorMessage", "unknown error")))

            print(f"  [{min(i + BATCH_SIZE, total)}/{total}] indexed so far: {indexed}")
            time.sleep(BATCH_PAUSE_SECONDS)
    finally:
        db.close()

    print("\n" + "=" * 78)
    print("BATCH EMBEDDING + INDEXING SUMMARY")
    print("=" * 78)
    print(f"Employees processed: {total}")
    print(f"Successfully indexed: {indexed}")
    print(f"Failures: {len(failures)}")
    for emp_id, name, reason in failures[:20]:
        print(f"  FAILED: {name} ({emp_id}): {reason}")
    if len(failures) > 20:
        print(f"  ...and {len(failures) - 20} more")

    if sample_doc_id:
        print("\n" + "-" * 78)
        print(f"SAMPLE INDEXED DOCUMENT (fetched back from the index, id={sample_doc_id})")
        print("-" * 78)
        doc = fetch_document(sample_doc_id)
        doc.pop("@odata.context", None)
        vector = doc.get("profile_vector")
        if vector:
            doc["profile_vector"] = f"[{len(vector)}-dim vector; first 5 values: {vector[:5]}]"
        print(json.dumps(doc, indent=2))
    else:
        print("\nNo document was successfully indexed — nothing to sample.")


if __name__ == "__main__":
    main()
