"""get_org_chain(person_id, direction, depth) — recursive org chart traversal.

Same pipeline and philosophy as find_people/get_person: retrieve -> filter
records -> (direction/RBAC check) -> cap results -> write audit_log ->
respond. The org chart is not exempt from any of it — upward (who this
person reports to) is visible to everyone who can see the record at all;
downward (who reports to this person) is restricted to manager and hr,
exactly like the "manager chain | upward: all, downward: manager+" row in
the field-visibility table. Insufficient role gets an empty list, same
redact-never-reject treatment as any other restricted field — the root
record itself is what 404s (mirroring get_person), not the direction.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Literal

from sqlalchemy import literal, select
from sqlalchemy.orm import Session, aliased

from app.auth import AuthenticatedUser
from app.models import AuditLog, Employee, OrgUnit
from app.people import MAX_RESULTS
from app.permissions import is_record_visible
from app.schemas import OrgChainNode

# The recursive CTE always stops here, no matter what depth is requested or
# how malformed the manager_id data is — this IS the cycle guard. A tight
# manager_id cycle (A -> B -> A) can't make the query hang: after MAX_DEPTH
# hops the WHERE clause in the recursive term simply stops adding rows,
# same as it would for a legitimately deep chain.
MAX_DEPTH = 10


def _org_unit_name(db: Session, org_unit_id: int) -> str:
    unit = db.get(OrgUnit, org_unit_id)
    return unit.name if unit else ""


def _write_audit(db: Session, caller: AuthenticatedUser, query_text: str, result_count: int) -> None:
    db.add(AuditLog(
        actor_id=caller.id, action="get_org_chain", query_text=query_text, result_count=result_count,
        fields_returned=json.dumps(["id", "full_name", "job_title", "org_unit", "depth"]),
        timestamp=datetime.now(),
    ))
    db.commit()


def _traverse(db: Session, start_id: str, direction: Literal["up", "down"], max_depth: int) -> list[tuple[str, int]]:
    """Recursive CTE walking employees.manager_id in the given direction.

    Dialect note: SQLite requires the `WITH RECURSIVE` keyword on the CTE;
    Azure SQL's T-SQL has no RECURSIVE keyword at all (recursive CTEs are
    just always allowed under plain `WITH`), but T-SQL does require the
    anchor and recursive SELECTs to line up on an explicit, unambiguous
    column list — satisfied here since every column is labeled. Both are
    otherwise standard ANSI recursive CTEs (anchor UNION ALL recursive
    term), and SQLAlchemy's `.cte(recursive=True)` already emits the right
    form per dialect, so nothing dialect-specific needs to be written by
    hand in this function.
    """
    anchor = (
        select(Employee.id, Employee.manager_id, literal(0).label("depth"))
        .where(Employee.id == start_id)
        .cte(name="org_chain", recursive=True)
    )
    emp = aliased(Employee)
    join_condition = (emp.id == anchor.c.manager_id) if direction == "up" else (emp.manager_id == anchor.c.id)
    recursive_term = (
        select(emp.id, emp.manager_id, (anchor.c.depth + 1).label("depth"))
        .join(anchor, join_condition)
        .where(anchor.c.depth < max_depth)
    )
    chain = anchor.union_all(recursive_term)

    rows = db.execute(
        select(chain.c.id, chain.c.depth).where(chain.c.depth > 0).order_by(chain.c.depth)
    ).all()
    return [(r.id, r.depth) for r in rows]


def get_org_chain(
    db: Session,
    caller: AuthenticatedUser,
    person_id: str,
    direction: Literal["up", "down"],
    depth: int = MAX_DEPTH,
) -> list[OrgChainNode] | None:
    effective_depth = max(1, min(depth, MAX_DEPTH))
    result: list[OrgChainNode] = []
    try:
        # 1. retrieve + 2. filter records — the root person itself. Same
        # "identical whether nonexistent or restricted" shape as get_person.
        root = db.get(Employee, person_id)
        if root is None or not root.is_active or not is_record_visible(caller, root):
            return None

        # Direction check (RBAC only — no relationship requirement, same
        # shape as hire_date/cost_centre being hr-only).
        if direction == "down" and caller.role not in ("manager", "hr"):
            return []

        raw = _traverse(db, person_id, direction, effective_depth)

        seen: set[str] = set()
        for emp_id, node_depth in raw:
            if emp_id in seen:
                continue  # a cycle can revisit the same person at a deeper level
            seen.add(emp_id)
            emp = db.get(Employee, emp_id)
            if emp is None or not emp.is_active or not is_record_visible(caller, emp):
                continue  # 3. filter records again, for every node in the chain
            result.append(OrgChainNode(
                id=emp.id, full_name=emp.full_name, job_title=emp.job_title,
                org_unit=_org_unit_name(db, emp.org_unit_id), depth=node_depth,
            ))

        # 4. cap results — a downward chain from someone near the top can
        # otherwise touch most of the company in one call.
        result = result[:MAX_RESULTS]
        return result
    finally:
        # 5. write to audit_log — logged whether visible, role-denied, or
        # fully populated; the audit trail always knows the truth.
        _write_audit(db, caller, f"person_id={person_id};direction={direction};depth={depth}", len(result))
    # 6. respond (via the returns above)
