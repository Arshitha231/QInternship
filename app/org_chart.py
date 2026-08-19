"""get_org_chain(person_id, direction, depth) — recursive org chart traversal.

Same pipeline and philosophy as find_people/get_person: retrieve -> filter
records -> (direction/RBAC check) -> cap results -> write audit_log ->
respond. The org chart is not exempt from any of it — upward (who this
person reports to) is visible to everyone who can see the record at all;
downward (who reports to this person) is restricted to manager and hr in
work mode, exactly like the "manager chain | upward: all, downward:
manager+" row in the field-visibility table — and to nobody in employee
mode, where every caller is an ordinary colleague. Insufficient role gets an empty list, same
redact-never-reject treatment as any other restricted field — the root
record itself is what 404s (mirroring get_person), not the direction.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from rapidfuzz import fuzz, process
from sqlalchemy import func, literal, or_, select
from sqlalchemy.orm import Session, aliased

from app.auth import AuthenticatedUser
from app.models import AuditLog, Employee, OrgUnit
from app.people import MAX_RESULTS
from app.permissions import ViewMode
from app.policy import can_see_direct_reports, enforce, excluded_by_obligations
from app.query_compiler import enforced_person_ref
from app.query_plan import PeopleQuery
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


@dataclass(frozen=True)
class PersonResolution:
    """The outcome of turning a typed name into one employee id.

    Three distinct outcomes, not two: resolved, ambiguous (several real
    people matched and picking one would answer a different question than
    the one asked), and unknown (nothing matched). The old `str | None`
    return collapsed the last two, which is why "Priya Sharma" — a genuinely
    duplicated name — and "Zzyzx Qqwrt" produced the identical, wrong
    "nobody found above them in the org chart" message.
    """

    person_id: str | None = None
    candidates: tuple[str, ...] = ()

    @property
    def is_ambiguous(self) -> bool:
        return self.person_id is None and bool(self.candidates)

    @property
    def is_unknown(self) -> bool:
        return self.person_id is None and not self.candidates


# How far below the top fuzzy score another person can score and still count
# as a rival rather than a clear loser. A real typo has one obvious winner
# ("Shaun Andersen" -> "Shaun Anderson" at ~96, next best in the fifties); a
# bare surname does not ("Anderson" scores identically against all five
# Andersons, because WRatio's partial pass sees a full substring hit in every
# one of them). Without this margin the resolver silently returned whichever
# of the five rapidfuzz happened to rank first.
AMBIGUITY_MARGIN = 5


def _name_index(db: Session) -> dict[str, list[str]]:
    """Every string a caller could reasonably use for an active employee ->
    the ids it could mean. Both full_name and preferred_name, because a
    directory that can't find "Andy" when the record says "Andrew Choi" is
    broken for exactly the phrasing colleagues actually use. 9 employees
    here have a real nickname and none of them were reachable by it before.
    """
    index: dict[str, list[str]] = {}
    rows = db.execute(
        select(Employee.id, Employee.full_name, Employee.preferred_name).where(Employee.is_active == True)  # noqa: E712
    ).all()
    for emp_id, full_name, preferred_name in rows:
        for candidate in (full_name, preferred_name):
            if not candidate:
                continue
            ids = index.setdefault(candidate, [])
            if emp_id not in ids:
                ids.append(emp_id)
    return index


def _decide(ids: list[str], _query: str) -> PersonResolution | None:
    """One matched id resolves; several is ambiguous; none defers to the
    next tier (None)."""
    unique = list(dict.fromkeys(ids))
    if len(unique) == 1:
        return PersonResolution(person_id=unique[0])
    if len(unique) > 1:
        return PersonResolution(candidates=tuple(unique))
    return None


def resolve_person(db: Session, name: str) -> PersonResolution:
    """Resolve a plain name to exactly one active employee, or say why not.

    Three tiers, tried in order — exact, case-insensitive, fuzzy — each over
    both full_name and preferred_name. A tier that matches several distinct
    people stops there and reports ambiguity rather than falling through to
    a looser tier, since a looser tier can only ever add more candidates.
    """
    name = name.strip()
    if not name:
        return PersonResolution()

    # Exact and case-insensitive stay indexed SQL lookups -- the common
    # case (a name typed or clicked correctly) must not pay for loading
    # every employee's names into memory. Only the fuzzy tier below needs
    # the full in-process index.
    exact = db.execute(
        select(Employee.id).where(
            or_(Employee.full_name == name, Employee.preferred_name == name),
            Employee.is_active == True,  # noqa: E712
        )
    ).scalars().all()
    if (decided := _decide(list(exact), name)) is not None:
        return decided

    lowered = name.lower()
    ci = db.execute(
        select(Employee.id).where(
            or_(func.lower(Employee.full_name) == lowered, func.lower(Employee.preferred_name) == lowered),
            Employee.is_active == True,  # noqa: E712
        )
    ).scalars().all()
    if (decided := _decide(list(ci), name)) is not None:
        return decided

    index = _name_index(db)
    scored = process.extract(
        name, list(index.keys()), scorer=fuzz.WRatio,
        score_cutoff=FUZZY_MATCH_THRESHOLD, limit=None,
    )
    if not scored:
        return PersonResolution()

    # Rivals are judged per PERSON, not per matched string: one employee can
    # appear twice (full_name and preferred_name both scoring) and that is
    # not ambiguity, it is the same person matching two ways.
    best_by_person: dict[str, float] = {}
    for matched_name, score, _index in scored:
        for emp_id in index[matched_name]:
            if score > best_by_person.get(emp_id, -1):
                best_by_person[emp_id] = score
    top = max(best_by_person.values())
    contenders = [emp_id for emp_id, score in best_by_person.items() if score >= top - AMBIGUITY_MARGIN]
    return _decide(contenders, name) or PersonResolution()


def resolve_person_name(db: Session, name: str) -> str | None:
    """Back-compatible wrapper: the id, or None for both "no match" and
    "too many matches". Callers that need to tell those apart — and every
    caller that phrases an answer for a human should — use resolve_person()
    directly.
    """
    return resolve_person(db, name).person_id


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
    view_mode: ViewMode = "work",
) -> list[OrgChainNode] | None:
    """view_mode was missing entirely until now, and that was a real hole
    rather than an omission of convenience: this function had no way to
    express employee mode, so an hr/manager caller previewing it kept the
    downward direction (who reports to this person) that an ordinary
    colleague never gets, and kept seeing restricted people in the chain.
    app.policy.can_see_direct_reports' own docstring recorded the drift --
    find_people collapsed via effective_role, this didn't.

    Both consequences are fixed by threading it through: enforce() picks up
    the restricted-record obligation for the mode, and the direction gate
    below finally receives it.
    """
    effective_depth = max(1, min(depth, MAX_DEPTH))
    result: list[OrgChainNode] = []
    try:
        # Row-level policy decision, computed once, for the caller's mode --
        # in employee mode this restores the restricted-record obligation
        # that hr would otherwise keep here alone.
        decision = enforce(PeopleQuery(select=["id"]), caller, view_mode)

        # 1. retrieve + 2. filter records — the root person itself. Same
        # "identical whether nonexistent or restricted" shape as get_person.
        root = db.get(Employee, person_id)
        if root is None or not root.is_active or excluded_by_obligations(decision, root):
            return None

        # Direction check (RBAC only — no relationship requirement, same
        # shape as hire_date/cost_centre being hr-only). The caller's real
        # view_mode goes in: can_see_direct_reports owns the hr-previewing-
        # employee-mode vs manager-who-has-no-work-mode distinction, so this
        # call site and find_people's enrichment get the same answer for the
        # same person without either having to re-derive it.
        if direction == "down" and not can_see_direct_reports(caller.role, view_mode):
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
            # 3. filter records again, for every node in the chain -- this
            # MUST stay a post-traversal per-node filter, never pushed into
            # _traverse()'s own CTE. The chain walks THROUGH a restricted
            # intermediate manager today (excluded from the output, but the
            # walk continues past them to whoever's above/below); excluding
            # them at the CTE's WHERE level instead would stop the walk
            # itself from ever reaching past them, silently losing everyone
            # beyond a restricted person in someone's chain -- a real
            # behavior change, not a refactor.
            if emp is None or not emp.is_active or excluded_by_obligations(decision, emp):
                continue
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
        # view_mode recorded too: the same request answers differently in
        # each lens now, so an audit row without it cannot explain its own
        # result_count.
        _write_audit(
            db, caller,
            f"person_id={person_id};direction={direction};depth={depth};view_mode={view_mode}",
            len(result),
        )
    # 6. respond (via the returns above)
