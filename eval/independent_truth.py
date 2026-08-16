"""Independent ground-truth queries for golden_set.py.

Deliberately does NOT call app.people.find_people, app.org_chart.
get_org_chain/_traverse, or app.directory_tools.find_mentor. Those are
exactly what the golden eval grades -- computing "the correct answer"
through the same function that produces "the system's answer" makes any
bug in that function invisible (it agrees with itself no matter what), and
can inject false failures from that function's own non-determinism (an
unordered-by-default DB query feeding a stable sort's tiebreak, e.g.) that
have nothing to do with NL routing quality. This module re-derives each
answer straight from app.models via its own queries/walks, written fresh --
not a copy-paste of the graded function's internals. A disagreement is now
a real signal: either the router picked the wrong tool/arguments, or the
graded function has an actual bug. Never "the same bug on both sides."

is_record_visible (app.permissions) is the one exception, reused rather
than reimplemented: it isn't what this eval grades (routing/retrieval is),
it already has its own dedicated fixture-based suite
(tests/test_visibility.py), and hand-duplicating RBAC/ABAC a second time
here would risk exactly the kind of drift app/registry.py exists to
prevent everywhere else.
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.auth import AuthenticatedUser
from app.models import Employee, EmployeeProject, EmployeeSkill, Office, OrgUnit, Project, Skill
from app.models.enums import AvailabilityStatus, ProjectClassification, SkillLevel
from app.permissions import is_record_visible

MAX_DEPTH = 10  # generous bound -- deepest real chain in seed data is 7 hops
MAX_MENTOR_RESULTS = 5  # independent of app.people.MAX_SEARCH_RESULTS on purpose


def _resolve_skill(db: Session, name: str) -> Skill | None:
    """Exact case-insensitive name match, then follow canonical_id if this
    row is an alias. Same shape as app.people.resolve_skill's synonym
    resolution but written fresh here, not imported from there."""
    skill = db.query(Skill).filter(Skill.name.ilike(name)).first()
    if skill is None:
        return None
    if skill.canonical_id is not None:
        return db.get(Skill, skill.canonical_id)
    return skill


def _resolve_name_exact(db: Session, name: str) -> str | None:
    """Exact (case-sensitive) full-name match only -- golden_set.py always
    supplies a real, correctly-spelled name as the "objectively correct"
    argument, so unlike org_chart.resolve_person_name this needs no
    fuzzy/case-insensitive fallback. A name matching zero or 2+ active
    employees is unresolved, not guessed."""
    matches = db.execute(
        select(Employee.id).where(Employee.full_name == name.strip(), Employee.is_active == True)
    ).scalars().all()
    return matches[0] if len(matches) == 1 else None


def _org_unit_and_descendants(db: Session, name: str) -> set[int]:
    """Plain Python BFS over OrgUnit.parent_id -- independent of
    app.people._org_unit_and_descendant_ids's recursive CTE."""
    root = db.query(OrgUnit).filter(OrgUnit.name.ilike(name)).first()
    if root is None:
        return set()
    ids = {root.id}
    frontier = [root.id]
    for _ in range(MAX_DEPTH):
        if not frontier:
            break
        children = db.execute(select(OrgUnit.id).where(OrgUnit.parent_id.in_(frontier))).scalars().all()
        new = [c for c in children if c not in ids]
        if not new:
            break
        ids.update(new)
        frontier = new
    return ids


def _walk_chain(db: Session, start_id: str, direction: str) -> set[str]:
    """Independent recursive walk over Employee.manager_id -- a plain
    Python BFS loop, not app.org_chart._traverse's SQL recursive CTE.
    direction="up": ancestors (managers). direction="down": descendants
    (reports, any depth). Does not include start_id itself."""
    seen: set[str] = {start_id}
    result: set[str] = set()
    frontier = [start_id]
    for _ in range(MAX_DEPTH):
        if not frontier:
            break
        if direction == "up":
            next_frontier = []
            for emp_id in frontier:
                emp = db.get(Employee, emp_id)
                if emp and emp.manager_id and emp.manager_id not in seen:
                    next_frontier.append(emp.manager_id)
        else:
            rows = db.execute(
                select(Employee.id).where(Employee.manager_id.in_(frontier), Employee.is_active == True)
            ).scalars().all()
            next_frontier = [i for i in rows if i not in seen]
        seen.update(next_frontier)
        result.update(next_frontier)
        frontier = next_frontier
    return result


def direct_reports(db: Session, caller: AuthenticatedUser, *, manager_name: str) -> set[str]:
    """Who reports directly (one hop) to the named manager, visible to
    caller. Same RBAC gate as find_people's enrichment (manager/hr only)
    and get_org_chain's downward direction -- not exercised differently by
    golden_set.py today (callers are always the manager themself), but
    replicated for correctness."""
    if caller.role not in ("manager", "hr"):
        return set()
    manager_id = _resolve_name_exact(db, manager_name)
    if manager_id is None:
        return set()
    rows = db.execute(
        select(Employee).where(Employee.manager_id == manager_id, Employee.is_active == True)
    ).scalars().all()
    return {e.id for e in rows if is_record_visible(caller, e)}


def org_chain(db: Session, caller: AuthenticatedUser, *, person_name: str, direction: str) -> set[str]:
    """Everyone above/below the named person in the reporting chain,
    filtered per-node the same way get_org_chain's docstring specifies:
    upward visible to everyone who can see the root record, downward
    restricted to manager/hr."""
    if direction == "down" and caller.role not in ("manager", "hr"):
        return set()
    root_id = _resolve_name_exact(db, person_name)
    if root_id is None:
        return set()
    root = db.get(Employee, root_id)
    if root is None or not root.is_active or not is_record_visible(caller, root):
        return set()
    chain_ids = _walk_chain(db, root_id, direction)
    result = set()
    for emp_id in chain_ids:
        emp = db.get(Employee, emp_id)
        if emp and emp.is_active and is_record_visible(caller, emp):
            result.add(emp_id)
    return result


def filter_people(
    db: Session, caller: AuthenticatedUser, *,
    name: str | None = None, skill: str | None = None, level: str | None = None,
    org_unit: str | None = None, office: str | list[str] | None = None, language: str | None = None,
    available: bool | None = None, job_title: str | None = None,
) -> set[str]:
    """Which visible, active employees match these criteria -- a direct
    join query, no ranking/caps/Search: just the set of people who are
    correct, independent of how find_people would go about retrieving
    them.

    `office` as a list and `job_title` exist for Piece 2's search_people
    ground truth (golden_set.py's compound-query cases, "Bangalore or
    Singapore" / "title contains Director") -- find_people itself has no
    parameter for either shape, so those two golden questions can only ever
    be answered through search_people's PeopleQuery path, not find_people's
    fixed parameters. Written fresh here rather than reusing
    app.query_compiler.apply_filter, same independence rule as every other
    criterion in this function (see module docstring)."""
    stmt = select(Employee).where(Employee.is_active == True)

    if name:
        stmt = stmt.where(func.lower(Employee.full_name) == name.strip().lower())

    if skill:
        resolved = _resolve_skill(db, skill)
        if resolved is None:
            return set()
        skill_subq = select(EmployeeSkill.employee_id).where(EmployeeSkill.skill_id == resolved.id)
        if level:
            lvl = next((m for m in SkillLevel if m.value.lower() == level.lower()), None)
            if lvl is None:
                return set()
            skill_subq = skill_subq.where(EmployeeSkill.level == lvl)
        stmt = stmt.where(Employee.id.in_(skill_subq))

    if language:
        resolved_lang = _resolve_skill(db, language)
        if resolved_lang is None:
            return set()
        lang_subq = select(EmployeeSkill.employee_id).where(EmployeeSkill.skill_id == resolved_lang.id)
        stmt = stmt.where(Employee.id.in_(lang_subq))

    if org_unit:
        unit_ids = _org_unit_and_descendants(db, org_unit)
        if not unit_ids:
            return set()
        stmt = stmt.where(Employee.org_unit_id.in_(unit_ids))

    if office:
        offices = office if isinstance(office, list) else [office]
        clauses = [or_(Office.name.ilike(f"%{o}%"), Office.city.ilike(f"%{o}%")) for o in offices]
        stmt = stmt.join(Office, Employee.office_id == Office.id).where(or_(*clauses))

    if job_title:
        stmt = stmt.where(Employee.job_title.ilike(f"%{job_title}%"))

    if available:
        stmt = stmt.where(Employee.availability_status == AvailabilityStatus.available)

    rows = db.execute(stmt).scalars().all()
    return {e.id for e in rows if is_record_visible(caller, e)}


def filter_people_or(db: Session, caller: AuthenticatedUser, *, groups: list[dict]) -> set[str]:
    """Ground truth for search_people's filter_groups (bounded DNF): the
    union of filter_people(**group) across `groups` -- each group is its
    own AND clause (reusing filter_people's existing per-field AND
    semantics unchanged), OR'd together by simple set union. Independent in
    the same sense as filter_people itself (module docstring): a plain
    Python union of per-group SQLAlchemy queries, not a call into
    app.query_compiler's own OR-of-AND compilation, which is exactly what
    these golden questions grade.
    """
    result: set[str] = set()
    for group in groups:
        result |= filter_people(db, caller, **group)
    return result


def find_mentor(db: Session, caller: AuthenticatedUser, *, skill: str, caller_id: str) -> list[str]:
    """Independent reimplementation of the *specified* ranking (per
    app.directory_tools.find_mentor's own docstring): expert > available >
    outside caller's chain > more shared non-confidential projects > longer
    tenure. Same rule, computed via this module's own queries/walks rather
    than by calling that function -- so a bug in the real implementation
    (wrong sign, chain excluded instead of de-prioritized, confidential
    projects counted, etc.) shows up as a disagreement instead of being
    invisible. Adds one thing the real function doesn't have: an explicit
    final tiebreak on employee id, so THIS ground truth is reproducible
    across runs even though the graded function's own tie order depends on
    undefined SQL row order -- see the module docstring.
    """
    resolved = _resolve_skill(db, skill)
    if resolved is None:
        return []

    rows = db.execute(
        select(EmployeeSkill, Employee)
        .join(Employee, EmployeeSkill.employee_id == Employee.id)
        .where(
            EmployeeSkill.skill_id == resolved.id,
            EmployeeSkill.level.in_([SkillLevel.working, SkillLevel.expert]),
            Employee.is_active == True,
            Employee.id != caller_id,
        )
    ).all()
    candidates = [(es, e) for es, e in rows if is_record_visible(caller, e)]
    if not candidates:
        return []

    chain_ids = _walk_chain(db, caller_id, "up") | _walk_chain(db, caller_id, "down")

    caller_project_ids = {
        row.project_id for row in db.execute(
            select(EmployeeProject.project_id)
            .join(Project, EmployeeProject.project_id == Project.id)
            .where(EmployeeProject.employee_id == caller_id,
                   Project.classification != ProjectClassification.confidential)
        )
    }

    def shared_project_count(emp_id: str) -> int:
        their_ids = {
            row.project_id for row in db.execute(
                select(EmployeeProject.project_id)
                .join(Project, EmployeeProject.project_id == Project.id)
                .where(EmployeeProject.employee_id == emp_id,
                       Project.classification != ProjectClassification.confidential)
            )
        }
        return len(caller_project_ids & their_ids)

    scored = []
    for es, e in candidates:
        is_expert = es.level == SkillLevel.expert
        is_available = e.availability_status == AvailabilityStatus.available
        in_chain = e.id in chain_ids
        shared = shared_project_count(e.id)
        tenure = (date.today() - e.hire_date).days
        sort_key = (0 if is_expert else 1, 0 if is_available else 1, 1 if in_chain else 0, -shared, -tenure, e.id)
        scored.append((sort_key, e.id))

    scored.sort(key=lambda pair: pair[0])
    return [emp_id for _key, emp_id in scored][:MAX_MENTOR_RESULTS]
