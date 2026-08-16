"""Populate project_embeddings — the corpus behind Mode 3 (find_experts).

Operator tool, same family as build_search_index.py and the seed_* scripts:
run it from the App Service SSH console against the deployed database, or
locally against directory.db. It needs EMBEDDING_ENDPOINT, EMBEDDING_KEY and
OPENAI_EMBEDDING_DEPLOYMENT in the environment.

Not a migration. Migrations run at app startup on every deploy; this makes
paid embedding calls and must be an explicit, deliberate step.

Idempotent by design — each project's embedded text is hashed, and a
project whose text hasn't changed is skipped. Re-running after editing one
description costs one embedding call, not 128. `--force` re-embeds
everything anyway (use after switching embedding models, where the stored
vectors are no longer comparable to new ones).

Confidential projects are never embedded. That exclusion lives in
app/project_search.embeddable_projects(), the single place that decides what
enters the corpus, so this script has no rule of its own to get wrong.
"""
from __future__ import annotations

import sys

from dotenv import load_dotenv

load_dotenv()

from app.db import SessionLocal  # noqa: E402
from app.models import Project, ProjectEmbedding  # noqa: E402
from app.project_search import (  # noqa: E402
    EmbeddingUnavailable,
    build_project_text,
    embeddable_projects,
    rebuild_embeddings,
    unpack_vector,
)
from app.search_client import (  # noqa: E402
    EMBEDDING_DEPLOYMENT,
    EMBEDDING_ENDPOINT,
    EMBEDDING_KEY,
)


def preflight() -> None:
    missing = [
        name for name, value in (
            ("EMBEDDING_ENDPOINT", EMBEDDING_ENDPOINT),
            ("EMBEDDING_KEY", EMBEDDING_KEY),
            ("OPENAI_EMBEDDING_DEPLOYMENT", EMBEDDING_DEPLOYMENT),
        ) if not value
    ]
    if missing:
        print("Cannot embed: missing " + ", ".join(missing))
        print("Set them in .env (locally) or the App Service settings (deployed) and re-run.")
        raise SystemExit(2)


def main() -> None:
    force = "--force" in sys.argv
    preflight()

    db = SessionLocal()
    try:
        projects = embeddable_projects(db)
        total_projects = db.query(Project).count()
        print(f"Corpus: {len(projects)} embeddable of {total_projects} projects "
              f"({total_projects - len(projects)} confidential, excluded by construction)")

        # A description that is still the old "<name> — owned by <unit>."
        # placeholder embeds to nothing useful — worth saying out loud
        # rather than quietly producing a corpus that can't answer anything.
        placeholder = [p for p in projects if (p.description or "").startswith(f"{p.name} — owned by")]
        if placeholder:
            print(f"WARNING: {len(placeholder)} project(s) still have placeholder descriptions. "
                  f"They will embed, but semantic search over them is close to useless.")

        print(f"Model: {EMBEDDING_DEPLOYMENT}{'  (--force: re-embedding everything)' if force else ''}")
        print("Embedding...", flush=True)

        try:
            stats = rebuild_embeddings(db, force=force)
        except EmbeddingUnavailable as exc:
            print(f"FAILED: {exc}")
            raise SystemExit(1)

        print(f"  embedded: {stats['embedded']}")
        print(f"  skipped (unchanged): {stats['skipped']}")
        print(f"  removed (now confidential or deleted): {stats['removed']}")

        rows = db.query(ProjectEmbedding).count()
        print(f"\nproject_embeddings now holds {rows} row(s).")

        sample = db.query(ProjectEmbedding).first()
        if sample:
            project = db.get(Project, sample.project_id)
            vector = unpack_vector(sample.vector)
            print("\n" + "-" * 78)
            print(f"SAMPLE ROW  project_id={sample.project_id}  {project.name!r}")
            print("-" * 78)
            print(f"model      : {sample.model}")
            print(f"dimensions : {sample.dimensions}")
            print(f"source_hash: {sample.source_hash[:16]}...")
            print(f"vector     : [{len(vector)}-dim, first 5: {[round(v, 5) for v in vector[:5]]}]")
            print(f"text       : {build_project_text(db, project)[:220]!r}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
