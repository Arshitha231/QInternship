"""Guards for SQLite-vs-Azure-SQL divergences the test suite cannot see.

Every test in this repo runs on SQLite. Azure SQL is stricter in ways SQLite
silently tolerates, so a query can pass all 562 tests and fail the moment it
touches the deployed database. That has now happened twice:

  `.is_(True)`   renders as `IS 1`, which T-SQL rejects outside a NULL
                 comparison. Fixed by convention (`== True`) and documented
                 in comments across app/people.py, app/org_chart.py,
                 app/continuity.py and the operator scripts.

  bare columns   SQLite permits a non-aggregated column in the select list
  in GROUP BY    that isn't in GROUP BY; T-SQL raises error 8120. This is
                 what broke `python seed_continuity.py` on the App Service:

                     Column 'skills.name' is invalid in the select list
                     because it is not contained in either an aggregate
                     function or the GROUP BY clause.

                 Caused by selecting a whole ORM entity while grouping by
                 one of its columns.

These tests assert the invariant structurally, against the real statements,
with no database connection of either kind.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest
from sqlalchemy.dialects import mssql
from sqlalchemy.sql.functions import Function

REPO_ROOT = Path(__file__).resolve().parent.parent


def _is_aggregate(column) -> bool:
    """True for count(...)/max(...)/etc., including when wrapped in a label."""
    inner = getattr(column, "element", column)
    return isinstance(inner, Function)


def assert_group_by_is_tsql_safe(stmt, label: str) -> None:
    """Every non-aggregated selected column must appear in GROUP BY."""
    grouped = {str(c) for c in stmt._group_by_clauses}
    if not grouped:
        return  # not a grouped query, nothing to check
    offenders = [
        str(col) for col in stmt.selected_columns
        if not _is_aggregate(col) and str(col) not in grouped
    ]
    assert not offenders, (
        f"{label}: {offenders} are selected but not grouped or aggregated. "
        f"SQLite allows this; Azure SQL raises error 8120. Select only the key "
        f"and re-fetch, or add the columns to GROUP BY."
    )


def test_widely_held_skill_query_is_tsql_safe():
    """The exact query that failed on the App Service."""
    from seed_continuity import widely_held_skill_stmt

    assert_group_by_is_tsql_safe(widely_held_skill_stmt(), "seed_continuity.widely_held_skill_stmt")


def test_widely_held_skill_query_compiles_for_mssql():
    """Belt and braces: the generated T-SQL selects only what it groups by."""
    from seed_continuity import widely_held_skill_stmt

    sql = str(widely_held_skill_stmt().compile(dialect=mssql.dialect()))
    select_list = sql[sql.index("SELECT") + 6:sql.index("FROM")]
    # skills.name / skills.category / skills.canonical_id were the columns
    # error 8120 named; only skills.id and the aggregate belong here.
    for column in ("skills.name", "skills.category", "skills.canonical_id"):
        assert column not in select_list, f"{column} is back in the select list"
    assert "skills.id" in select_list
    assert "GROUP BY skills.id" in sql


def _is_true_false_calls(source: str) -> list[int]:
    """Line numbers of real `.is_(True)` / `.is_(False)` CALLS.

    Parsed via ast rather than matched with a regex over the text: this
    repo discusses the pattern extensively in comments and docstrings (it
    has bitten twice), and a text-level guard flags every one of those
    explanations as a violation. The AST only sees executable code, so
    documenting the hazard can't trip the check that enforces it.

    `.is_(None)` is correct SQL and is deliberately not matched.
    """
    tree = ast.parse(source)
    hits = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "is_"
                and len(node.args) == 1
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value in (True, False)):
            hits.append(node.lineno)
    return hits


def test_no_is_true_in_query_code():
    """The other known divergence. `.is_(True)`/`.is_(False)` render as
    `IS 1`/`IS 0`, which T-SQL rejects outside a NULL comparison."""
    offenders: list[str] = []
    roots = [REPO_ROOT / "app"] + sorted(REPO_ROOT.glob("*.py"))
    for path in roots:
        files = path.rglob("*.py") if path.is_dir() else [path]
        for file in files:
            if "__pycache__" in str(file):
                continue
            for lineno in _is_true_false_calls(file.read_text()):
                offenders.append(f"{file.relative_to(REPO_ROOT)}:{lineno}")
    assert not offenders, (
        f"`.is_(True)`/`.is_(False)` renders as `IS 1`/`IS 0`, which Azure SQL rejects. "
        f"Use `== True` / `== False` instead. Found at: {offenders}"
    )


def test_the_is_true_guard_actually_detects_the_pattern():
    """The guard above passing could mean 'no violations' or 'the detector
    is broken'. This distinguishes them, and pins the is_(None) exemption."""
    assert _is_true_false_calls("q.filter(Employee.is_active.is_(True))") == [1]
    assert _is_true_false_calls("q.filter(Employee.is_active.is_(False))") == [1]
    assert _is_true_false_calls("q.filter(Employee.manager_id.is_(None))") == []
    assert _is_true_false_calls('"""prose mentioning .is_(True) in a docstring"""') == []
    assert _is_true_false_calls("q.filter(Employee.is_active == True)  # noqa") == []


@pytest.mark.parametrize("stmt_factory,label", [
    (lambda: __import__("seed_continuity").widely_held_skill_stmt(), "seed_continuity"),
])
def test_registered_statements_stay_portable(stmt_factory, label):
    """Extension point: as other grouped queries get factored out for
    inspection, add them here rather than writing a new test each time."""
    assert_group_by_is_tsql_safe(stmt_factory(), label)
