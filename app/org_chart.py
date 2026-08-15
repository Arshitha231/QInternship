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

from rapidfuzz import fuzz, process
from sqlalchemy import func, literal, select
from sqlalchemy.orm import Session, aliased

from app.auth import AuthenticatedUser
from app.models import AuditLog, Employee, OrgUnit
from app.people import MAX_RESULTS
from app.permissions import is_record_visible
from app.query_compiler import enforced_person_ref
from app.schemas import OrgChainNode

# The recursive CTE always stops here, no matter what depth is requested or
# how malformed the manager_id data is — this IS the cycle guard. A tight
# manager_id cycle (A -> B -> A) can't make the query hang: after MAX_DEPTH
# hops the WHERE clause in the recursive term simply stops adding rows,
# same as it would for a legitimately deep chain.
MAX_DEPTH = 10

# Below this rapidfuzz score (0-100), a name is unresolvable rather than a
# guess — ARCHITECTURE_2.md §11/RC2: the model gets a UUID-shaped tool
# argument to fill in, but users ask by name ("who is above Shaun
# Anderson"), and there was no resolver between the two. Chosen to catch
# real typos ("Shon Wilson" -> "Sean Wilson") without matching two
# unrelated short names against each other.
FUZZY_MATCH_THRESHOLD = 80


def resolve_person_name(db: Session, name: str) -> str | None:
    """Resolve a plain name string to exactly one active employee id: exact
    match, then case-insensitive, then fuzzy (rapidfuzz) above
    FUZZY_MATCH_THRESHOLD. In-process over every active employee's name —
    500 rows, not worth a fuzzy-search DB feature for.

    Two employees can legitimately share an exact full name (the seeded
    "Priya Sharma" duplicate) — the org chain needs exactly one root to
    walk from, and picking either one silently would answer a different
    question than the one asked, so an exact match that isn't unique is
    treated as unresolved, same as no match at all. Callers get "I
    couldn't find that person" either way, never a wrong-but-confident
    chain.
    """
    name = name.strip()
    if not name:
        return None

    exact = db.execute(
        select(Employee.id).where(Employee.full_name == name, Employee.is_active == True)
    ).scalars().all()
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return None

    ci = db.execute(
        select(Employee.id).where(func.lower(Employee.full_name) == name.lower(), Employee.is_active == True)
    ).scalars().all()
    if len(ci) == 1:
        return ci[0]
    if len(ci) > 1:
        return None

    active_names = db.execute(
        select(Employee.id, Employee.full_name).where(Employee.is_active == True)
    ).all()
    by_name: dict[str, list[str]] = {}
    for emp_id, full_name in active_names:
        by_name.setdefault(full_name, []).append(emp_id)

    match = process.extractOne(name, by_name.keys(), scorer=fuzz.WRatio, score_cutoff=FUZZY_MATCH_THRESHOLD)
    if match is None:
        return None
    matched_name, _score, _index = match
    matched_ids = by_name[matched_name]
    return matched_ids[0] if len(matched_ids) == 1 else None


def _org_unit_name(db: Session, org_unit_id: int) -> str:
    unit = db.get(OrgUnit, org_unit_id)
    return unit.name if unit else ""


def _write_audit(db: Session, caller: AuthenticatedUser, query_text: str, result_count: int) -> None:
    db.add(AuditLog(
        actor_id=caller.id, action="get_org_chain", query_text=query_text, result_count=result_count,
        fields_returned=json.dumps(
            ["id", "full_name", "job_title", "org_unit", "depth", "availability_status", "delegate", "has_reports"]
        ),
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


def manager_chain_ids(db: Session, employee_id: str, levels: int = -1) -> list[str]:
    """Everyone above `employee_id`, nearest first — ids only.

    The raw graph walk, with no permission filtering, no capping and no
    audit entry, for callers that ARE the permission decision (field
    visibility) or that apply their own rules per recipient afterwards
    (notification fan-out). get_org_chain above stays the only caller-facing
    traversal; this is the shared primitive underneath, so "the reporting
    chain" means one thing in this codebase.

    `levels` < 0 means unlimited, still bounded by MAX_DEPTH — the cycle
    guard is not negotiable by a config value. Duplicates from a cyclic
    manager_id are dropped, same as get_org_chain does.
    """
    depth = MAX_DEPTH if levels < 0 else min(levels, MAX_DEPTH)
    if depth <= 0:
        return []

    ordered: list[str] = []
    seen: set[str] = {employee_id}
    for emp_id, _node_depth in _traverse(db, employee_id, "up", depth):
        if emp_id in seen:
            continue
        seen.add(emp_id)
        ordered.append(emp_id)
    return ordered


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

        # One query up front rather than a per-node existence check — tells
        # every node in this response, in O(1), whether it has any reports
        # of its own (used by the frontend to show/hide an expand control).
        managers_with_reports: set[str] = set(
            db.execute(
                select(Employee.manager_id).where(
                    Employee.manager_id.is_not(None),
                    # `.is_(True)` renders as `IS 1` on SQL Server, which
                    # T-SQL rejects (IS only works with NULL) -- `== True`
                    # renders as `= 1` everywhere, including here.
                    Employee.is_active == True
                ).distinct()
            ).scalars().all()
        )

        seen: set[str] = set()
        for emp_id, node_depth in raw:
            if emp_id in seen:
                continue  # a cycle can revisit the same person at a deeper level
            seen.add(emp_id)
            emp = db.get(Employee, emp_id)
            if emp is None or not emp.is_active or not is_record_visible(caller, emp):
                continue  # 3. filter records again, for every node in the chain
            # manager/delegate: policy-gated via enforce()+compile_query(),
            # not a raw db.get() -- ARCHITECTURE_2.md §15 item 6 / Phase 3
            # Round 2 (app/query_compiler.py's enforced_person_ref), same
            # fix as find_people's single-match enrichment and
            # get_person's _build_detail().
            delegate = enforced_person_ref(db, caller, emp.delegate_id) if emp.delegate_id else None
            result.append(OrgChainNode(
                id=emp.id, full_name=emp.full_name, job_title=emp.job_title,
                org_unit=_org_unit_name(db, emp.org_unit_id), depth=node_depth,
                availability_status=emp.availability_status.value, delegate=delegate,
                has_reports=emp.id in managers_with_reports,
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
from sqlalchemy import select
from app.models import EmployeeProject, Project, Employee
from app.schemas import TeamGraphResponse, TeamProjectOut, TeammateOut, PersonRef, OrgChainNode
from app.permissions import can_see_confidential_project, is_record_visible
from app.people import MAX_RESULTS

def get_team_graph(
    db: Session,
    caller: AuthenticatedUser,
    person_id: str
) -> TeamGraphResponse | None:
    projects_out = []
    teammates_out = []
    
    try:
        # 1. Retrieve & verify root person visibility
        root = db.get(Employee, person_id)
        if root is None or not root.is_active or not is_record_visible(caller, root):
            return None

        # 2. Find their active projects (SQLAlchemy 2.0 syntax)
        my_eps = db.execute(
            select(EmployeeProject)
            .where(
                EmployeeProject.employee_id == person_id,
                EmployeeProject.end_date.is_(None)
            )
        ).scalars().all()
        
        seen_teammates_for_project = set()

        for ep in my_eps:
            proj = db.get(Project, ep.project_id)
            if not proj:
                continue
                
            # SECURITY CHECK: Hide confidential project edges from non-members[cite: 1]
            if proj.classification == "confidential" and not can_see_confidential_project(db, caller, proj.id):
                continue
                
            projects_out.append(
                TeamProjectOut(id=proj.id, name=proj.name, classification=proj.classification)
            )

            # 3. Find other active members on this specific project
            peers = db.execute(
                select(EmployeeProject)
                .where(
                    EmployeeProject.project_id == proj.id,
                    EmployeeProject.employee_id != person_id,
                    EmployeeProject.end_date.is_(None)
                )
            ).scalars().all()

            for peer_ep in peers:
                peer = db.get(Employee, peer_ep.employee_id)
                # Apply standard visibility rules to the peer
                if peer and peer.is_active and is_record_visible(caller, peer):
                    cache_key = f"{proj.id}-{peer.id}"
                    if cache_key not in seen_teammates_for_project:
                        seen_teammates_for_project.add(cache_key)
                        
                        delegate = None
                        if peer.delegate_id:
                            delegate_emp = db.get(Employee, peer.delegate_id)
                            if delegate_emp:
                                delegate = PersonRef(id=delegate_emp.id, full_name=delegate_emp.full_name)
                        
                        peer_node = OrgChainNode(
                            id=peer.id, 
                            full_name=peer.full_name, 
                            job_title=peer.job_title,
                            org_unit=_org_unit_name(db, peer.org_unit_id), 
                            depth=1,  # 1 hop away via the project
                            availability_status=peer.availability_status.value, 
                            delegate=delegate,
                            has_reports=False
                        )
                        
                        teammates_out.append(TeammateOut(project_id=proj.id, person=peer_node))

        # 4. Cap results to prevent bulk extraction[cite: 1, 4]
        teammates_out = teammates_out[:MAX_RESULTS]
        return TeamGraphResponse(projects=projects_out, teammates=teammates_out)
        
    finally:
        # 5. Write to audit_log — happens whether visible, role-denied, or populated[cite: 1, 4]
        _write_audit(db, caller, f"action=get_team_graph;person_id={person_id}", len(teammates_out))
