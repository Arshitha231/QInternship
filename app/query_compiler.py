"""Compiles a PeopleQuery + PolicyDecision into a read-only, parameterized
SQLAlchemy query over Employee rows (ARCHITECTURE_2.md §11, Phase 3 Round
2): approved columns only, parameterized values throughout (every value
comes from Filter.value or a resolved id, never string-formatted into SQL),
a hard row cap from the policy decision. No writes -- this module only ever
returns a SELECT.

This is the one place a PeopleQuery becomes SQL. compile_query() applies
`decision.required_filters` (obligations) exactly like the plan's own
filters -- appended unconditionally, never negotiable by the caller, since
they were never on the plan the model saw in the first place.

Field coverage matches app/registry.py's *filterable* set today: id,
full_name, preferred_name, org_unit, work_email, office, availability_status,
skills, languages. The org_unit hierarchy walk and skill/language resolution
mirror app.people's existing `_org_unit_and_descendant_ids` /
`resolve_skill` (same recursive-CTE / synonym-following shape) -- ported
here as this module's own copy rather than imported, since this is meant to
become the canonical version find_people's structured-filter branch adopts
later, not a second implementation that has to agree with a first one
forever. find_people keeps its own copies until that migration happens;
this module changes nothing about find_people's current behavior.
"""
from __future__ import annotations

from sqlalchemy import Select, func, literal, or_, select, union
from sqlalchemy.orm import aliased

from app.auth import AuthenticatedUser
from app.models import Employee, EmployeeSkill, Office, OrgUnit, Skill
from app.permissions import ViewMode
from app.policy import PolicyDecision, enforce
from app.query_plan import Filter, PeopleQuery
from app.registry import REGISTRY
from app.schemas import PersonRef

ORG_UNIT_MAX_DEPTH = 10  # mirrors app.people.ORG_UNIT_MAX_DEPTH

# order_by is restricted to columns that live directly on Employee -- the
# derived fields among the filterable set (org_unit/office come from a
# joined table via org_unit_id/office_id; skills/languages are a list, not
# an orderable scalar at all) don't have a plain column to sort on the way
# a WHERE clause can express them via a subquery. Widening this is a real
# feature addition (a join or a correlated subquery in the ORDER BY), not
# a bug fix, so it's deferred rather than silently wrong. Public (no
# leading underscore) since app.tool_calling's search_people tool schema
# also reads this, to build its order_by enum from the same source of
# truth compile_query() itself enforces, instead of a second hand-typed list.
ORDERABLE_FIELDS = frozenset({"id", "full_name", "preferred_name", "work_email", "availability_status"})


def _resolve_skill_id(db, name: str) -> int | None:
    """Case-insensitive lookup, following canonical_id if `name` is an
    alias -- same resolution app.people.resolve_skill performs, ported
    here rather than imported (see module docstring)."""
    skill = db.query(Skill).filter(Skill.name.ilike(name)).first()
    if skill is None:
        return None
    return skill.canonical_id if skill.canonical_id is not None else skill.id


def _org_unit_and_descendant_ids(db, name: str) -> list[int]:
    root = db.query(OrgUnit).filter(OrgUnit.name.ilike(name)).first()
    if root is None:
        return []
    anchor = (
        select(OrgUnit.id, OrgUnit.parent_id, literal(0).label("depth"))
        .where(OrgUnit.id == root.id)
        .cte(name="query_compiler_org_unit_tree", recursive=True)
    )
    child = aliased(OrgUnit)
    recursive_term = (
        select(child.id, child.parent_id, (anchor.c.depth + 1).label("depth"))
        .join(anchor, child.parent_id == anchor.c.id)
        .where(anchor.c.depth < ORG_UNIT_MAX_DEPTH)
    )
    tree = anchor.union_all(recursive_term)
    return [r.id for r in db.execute(select(tree.c.id)).all()]


class UnsupportedFilterError(ValueError):
    """A Filter's field/op combination isn't compilable yet -- deliberately
    a distinct exception from a plain ValueError, so a caller can choose to
    treat "this filter shape isn't supported" differently from a generic
    bug. Should never fire for a plan that already passed
    app.vocabulary.validate() against today's REGISTRY.filterable set."""


def apply_filter(db, stmt: Select, f: Filter) -> Select:
    """Applies one Filter to any Employee-mapped Select -- not just the
    statements compile_query() itself builds. Reused directly by
    app/people.py's find_people (its own hand-built SQL branch and
    direct_reports subquery) to push a PolicyDecision's obligations straight
    into a query, without adopting compile_query()'s full plan-driven
    pipeline for find_people's other, bespoke filter args. Public (no leading
    underscore) specifically because it's now used across module boundaries,
    not just internally by compile_query() below.
    """
    spec = REGISTRY[f.field]
    if not spec.filterable:
        raise UnsupportedFilterError(f"'{f.field}' is not filterable")

    if f.field in ("id", "full_name", "preferred_name", "work_email", "availability_status"):
        column = getattr(Employee, f.field)
        if f.op == "eq":
            return stmt.where(func.lower(column) == f.value.lower() if isinstance(f.value, str) else column == f.value)
        if f.op == "ne":
            return stmt.where(column != f.value)
        if f.op == "in":
            return stmt.where(column.in_(f.value))
        if f.op == "contains":
            return stmt.where(column.ilike(f"%{f.value}%"))
        raise UnsupportedFilterError(f"op '{f.op}' not compilable for '{f.field}'")

    if f.field == "org_unit":
        names = f.value if isinstance(f.value, list) else [f.value]
        unit_ids: set[int] = set()
        for name in names:
            unit_ids.update(_org_unit_and_descendant_ids(db, name))
        if not unit_ids:
            return stmt.where(literal(False))
        return stmt.where(Employee.org_unit_id.in_(unit_ids))

    if f.field == "office":
        values = f.value if isinstance(f.value, list) else [f.value]
        # `.ilike(v)` with no wildcards is a plain case-insensitive equality
        # match in SQL, not a substring one -- correct as-is for eq/in, but
        # this branch used to apply it unconditionally, so a model-supplied
        # `contains` silently behaved exactly like `eq` instead of actually
        # substring-matching. Latent since nothing called this live before
        # Piece 2 (the model-emitted PeopleQuery path); fixed as part of
        # giving it its first real caller, not discovered later as a
        # confusing bug report.
        if f.op == "contains":
            clauses = [or_(Office.name.ilike(f"%{v}%"), Office.city.ilike(f"%{v}%")) for v in values]
        elif f.op in ("eq", "in"):
            clauses = [or_(Office.name.ilike(v), Office.city.ilike(v)) for v in values]
        else:
            raise UnsupportedFilterError(f"op '{f.op}' not compilable for '{f.field}'")
        return stmt.join(Office, Employee.office_id == Office.id).where(or_(*clauses))

    if f.field == "job_title":
        # contains only (REGISTRY.ops for job_title) -- "title contains
        # Architect", not an exact match, which isn't a realistic ask.
        if f.op != "contains":
            raise UnsupportedFilterError(f"op '{f.op}' not compilable for '{f.field}'")
        return stmt.where(Employee.job_title.ilike(f"%{f.value}%"))

    if f.field in ("skills", "languages"):
        names = f.value if isinstance(f.value, list) else [f.value]
        skill_ids = [sid for name in names if (sid := _resolve_skill_id(db, name)) is not None]
        if not skill_ids:
            return stmt.where(literal(False))
        skill_subq = select(EmployeeSkill.employee_id).where(EmployeeSkill.skill_id.in_(skill_ids))
        return stmt.where(Employee.id.in_(skill_subq))

    raise UnsupportedFilterError(f"'{f.field}' has no compiler rule yet")


def _compile_group_ids(db, group: list[Filter], required_filters: list[Filter]) -> Select:
    """One OR-branch of plan.filter_groups, compiled as its own
    id-selecting Select -- a fresh statement per group, not a shared one, so
    each group's own joins (apply_filter's "office" branch, e.g.) stay
    scoped to that branch and never leak into or get skipped by another.

    `required_filters` (policy obligations) are applied INSIDE every group,
    exactly the same way compile_query() applies them to the top-level
    plan.filters -- not just once at the outer AND. Obligations also still
    get applied at the outer level below (harmless redundancy, AND is
    idempotent), but embedding them here too means a restricted row is
    excluded structurally by every single branch, not only "in aggregate"
    by an AND-distributes-over-OR argument a future refactor of the outer
    query shape could accidentally invalidate.
    """
    stmt = select(Employee.id)
    for f in [*group, *required_filters]:
        stmt = apply_filter(db, stmt, f)
    return stmt


def compile_query(db, plan: PeopleQuery, decision: PolicyDecision) -> Select:
    """Returns an unexecuted Select(Employee.id) -- ids only. Field-level
    shaping into a response object stays exactly where it already lives in
    every existing service function; this only decides WHICH rows, not
    what's returned about them (that's `decision.dropped_fields`, applied
    by the caller, same as today).
    """
    if not decision.allow:
        raise ValueError("compile_query called on a denied PolicyDecision -- check decision.allow first")

    stmt = select(Employee.id).where(Employee.is_active == True)  # noqa: E712

    for f in [*plan.filters, *decision.required_filters]:
        stmt = apply_filter(db, stmt, f)

    if plan.filter_groups:
        # Bounded DNF: filters (above) is an unconditional AND constraint;
        # filter_groups layers an OR-of-AND on top of it. Each group is its
        # own id-selecting Select (see _compile_group_ids) so per-group
        # joins never collide; a UNION of those id sets IS the OR, since
        # membership in the union means "matched at least one group."
        group_selects = [_compile_group_ids(db, group, decision.required_filters) for group in plan.filter_groups]
        group_ids = group_selects[0] if len(group_selects) == 1 else union(*group_selects)
        stmt = stmt.where(Employee.id.in_(group_ids))

    if plan.order_by is not None:
        if plan.order_by not in ORDERABLE_FIELDS:
            raise UnsupportedFilterError(f"order_by on '{plan.order_by}' is not compilable yet")
        stmt = stmt.order_by(getattr(Employee, plan.order_by))

    return stmt.limit(decision.max_rows)


def enforced_person_ref(
    db, caller: AuthenticatedUser, person_id: str, view_mode: ViewMode = "work",
) -> PersonRef | None:
    """The canonical way to attach a *referenced* person (a manager,
    delegate, ...) to a response -- policy-gated, not a raw db.get().
    ARCHITECTURE_2.md §15 item 6 / Round 2: find_people's single-match
    enrichment, get_person's _build_detail(), and get_org_chain's per-node
    delegate attachment all used to fetch the referenced person with no
    is_record_visible check at all -- currently unexploitable only because
    no restricted employee happens to manage or delegate for anyone in the
    seed data, but one seed change away from leaking a restricted person's
    name to a non-hr caller. This is every one of those three call sites
    routed through the same enforce() -> compile_query() pipeline as any
    other retrieval instead of three individual raw lookups, so a future
    obligation (a department scope, say) protects all three automatically
    instead of needing to be remembered at each site.

    `view_mode` defaults to "work" -- callers that don't have one to give
    (get_org_chain, which has no view_mode parameter at all) get today's
    unchanged behavior. find_people/get_person pass their real one: without
    it, enforce()'s restricted-record obligation always evaluated as if the
    caller were in full work mode, so an hr caller previewing "employee"
    mode would still see a restricted employee's name via a manager/delegate
    reference even though every other field on the same response was
    correctly anonymized.

    Builds the smallest possible PeopleQuery (select id+full_name, filter
    id eq person_id), enforces it, and returns None if policy denies it,
    the row is excluded by an obligation (restricted, non-hr caller), or
    the row doesn't exist -- same redact-never-reveal shape as everywhere
    else, never an error.
    """
    plan = PeopleQuery(select=["id", "full_name"], filters=[Filter(field="id", op="eq", value=person_id)])
    decision = enforce(plan, caller, view_mode)
    if not decision.allow:
        return None
    row_id = db.execute(compile_query(db, plan, decision)).scalar()
    if row_id is None:
        return None
    person = db.get(Employee, row_id)
    if person is None:
        return None
    return PersonRef(id=person.id, full_name=person.full_name)
