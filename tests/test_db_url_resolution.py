"""A relative SQLite DATABASE_URL must anchor to the project root, not cwd.

Regression test for a genuinely nasty failure mode: `sqlite:///directory.db`
resolves against the working directory, and SQLite does not error on a
missing file — it creates an empty one. So running anything from a
subdirectory (`cd eval && python run_golden_eval.py`, a seed script,
build_search_index.py) silently produced a brand-new empty database and then
failed several frames later with "no such table: employees", an error that
points at nothing useful and looks like a broken migration.

Pure function tests, no engine construction: app.db builds its engine at
import time from the module-level DATABASE_URL, so the resolution logic is
what's worth pinning down, and it's the part that has to leave Azure SQL and
the test suite's own temp-file URL alone.
"""
from pathlib import Path

import pytest

from app.db import _PROJECT_ROOT, _resolve


def test_relative_sqlite_path_anchors_to_project_root():
    resolved = _resolve("sqlite:///directory.db")
    assert resolved == f"sqlite:///{_PROJECT_ROOT / 'directory.db'}"
    # Four slashes: sqlite's own spelling for an absolute path.
    assert resolved.startswith("sqlite:////")


def test_nested_relative_path_anchors_too():
    assert _resolve("sqlite:///sub/dir/x.db") == f"sqlite:///{_PROJECT_ROOT / 'sub/dir/x.db'}"


@pytest.mark.parametrize("url", [
    # Already absolute — including the shape tests/conftest.py builds from
    # tempfile.mkstemp, which must keep pointing at the temp file and never
    # at a file inside the repo.
    "sqlite:////tmp/throwaway.db",
    "sqlite:///:memory:",
    "mssql+pymssql://user:pass@host:1433/directory",
    "postgresql://user:pass@host/directory",
])
def test_untouched(url):
    assert _resolve(url) == url


def test_the_test_suites_own_url_survives_resolution():
    """The suite would silently start writing into the repo if this broke."""
    import os

    current = os.environ["DATABASE_URL"]
    assert _resolve(current) == current
    assert Path(current.removeprefix("sqlite:///")).is_absolute()
