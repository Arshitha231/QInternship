"""Exports the current DATABASE_URL database to eval/fixture.db -- the
golden eval's pinned dataset (see eval/golden_set.py's module docstring for
why it's pinned rather than reseeded).

This is a deliberate, reviewed action, not something that runs automatically:
re-running it replaces the fixture every question in golden_set.py was
verified against. Before running this, confirm golden_set.py's personas and
hardcoded facts still resolve against whatever database you're about to
export -- eval/run_golden_eval.py itself will surface a broken resolution
as a crash or a nonsense score, but it won't tell you WHY, whereas checking
first (query directly, or adapt scripts/regen_golden_uuids.py's approach)
does.

Uses sqlite3's own backup API rather than a raw file copy -- safe against a
mid-write WAL/journal state even though this source db isn't usually under
concurrent load, and it's exactly as simple.

    python scripts/export_fixture.py [source_db_path]

source_db_path defaults to ./directory.db (DATABASE_URL's own default).
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_PATH = REPO_ROOT / "eval" / "fixture.db"


def main() -> None:
    source_path = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO_ROOT / "directory.db"
    if not source_path.exists():
        sys.exit(f"export_fixture: source database not found: {source_path}")

    source = sqlite3.connect(str(source_path))
    FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if FIXTURE_PATH.exists():
        FIXTURE_PATH.unlink()
    dest = sqlite3.connect(str(FIXTURE_PATH))
    with dest:
        source.backup(dest)
    source.close()
    dest.close()

    size_kb = FIXTURE_PATH.stat().st_size / 1024
    print(f"Exported {source_path} -> {FIXTURE_PATH} ({size_kb:.0f} KB)")
    print("Remember: `git add -f eval/fixture.db` -- it's *.db-ignored by default.")


if __name__ == "__main__":
    main()
