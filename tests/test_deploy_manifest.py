"""The deploy zip's include list is hand-maintained, and has silently
dropped a required file four times now (seed_training.py, seed_people_data.py,
backfill_project_history.py, then config/continuity_thresholds.yml).

Every one of those failed the same way: the deploy went green, /health stayed
200, and the missing piece only surfaced when someone hit the one feature that
needed it. config/ was the worst of the four -- it 500s every /continuity/*
route at request time while the rest of the API stays healthy.

These tests fail the build instead. They assert against the REAL resolved
paths the app reads at runtime, not a second hardcoded list that could drift
out of sync with the first one.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci-cd.yml"


def _zip_included_paths() -> set[str]:
    """The top-level paths passed to `zip -r deploy.zip ...`, with the
    trailing -x exclusion patterns and shell line continuations stripped."""
    text = WORKFLOW.read_text()
    match = re.search(r"zip -r deploy\.zip(.*?)(?:\n\s*\n|\n\s*- )", text, re.S)
    assert match, "could not find the `zip -r deploy.zip` command in ci-cd.yml"

    body = match.group(1).replace("\\\n", " ")
    tokens: list[str] = []
    for token in body.split():
        if token == "-x":  # everything after -x is an exclusion pattern
            break
        tokens.append(token)
    # Only the first path component matters -- `zip -r` ships whole trees.
    return {t.split("/")[0] for t in tokens if not t.startswith("-")}


# Files/dirs the running application reads. Operator-only scripts
# (seed_*.py, build_search_index.py) are deliberately NOT here: they're
# shipped for SSH-console use, but forgetting one doesn't take a route down.
RUNTIME_REQUIRED = ["app", "alembic", "alembic.ini", "requirements.txt", "config"]


@pytest.mark.parametrize("required", RUNTIME_REQUIRED)
def test_runtime_dependency_is_in_the_deploy_zip(required: str) -> None:
    included = _zip_included_paths()
    assert required in included, (
        f"{required!r} is read at runtime but is not in ci-cd.yml's deploy zip. "
        f"Currently shipping: {sorted(included)}"
    )


def test_continuity_thresholds_file_actually_ships() -> None:
    """Ties the manifest check to the path app/config.py really resolves,
    so moving the YAML can't quietly slip past the list above."""
    from app.config import continuity_thresholds_path

    # The env override is a test/runtime affordance, not what deploys use.
    os.environ.pop("CONTINUITY_THRESHOLDS_PATH", None)
    resolved = continuity_thresholds_path()

    assert resolved.is_file(), f"{resolved} does not exist in the repo"
    top_level = resolved.relative_to(REPO_ROOT).parts[0]
    assert top_level in _zip_included_paths(), (
        f"{resolved.relative_to(REPO_ROOT)} is read on every /continuity/* request, "
        f"but its top-level path {top_level!r} is not in the deploy zip"
    )


def test_thresholds_resolve_independently_of_cwd(tmp_path, monkeypatch) -> None:
    """The original default was the bare relative 'config/...yml', which only
    worked when the process happened to start from the repo root. The App
    Service's startup command is not required to."""
    from app.config import continuity_thresholds_path

    monkeypatch.delenv("CONTINUITY_THRESHOLDS_PATH", raising=False)
    monkeypatch.chdir(tmp_path)
    assert continuity_thresholds_path().is_file()
