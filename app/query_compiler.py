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

from sqlalchemy import Select, func, literal, or_, select
from sqlalchemy.orm import aliased

from app.auth import AuthenticatedUser
from app.models import Employee, EmployeeSkill, Office, OrgUnit, Skill
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
# a bug fix, so it's deferred rather than silently wrong.
_ORDERABLE_FIELDS = frozenset({"id", "full_name", "preferred_name", "work_email", "availability_status"})


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


def _apply_filter(db, stmt: Select, f: Filter) -> Select:
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
        clauses = [or_(Office.name.ilike(v), Office.city.ilike(v)) for v in values]
        return stmt.join(Office, Employee.office_id == Office.id).where(or_(*clauses))

    if f.field in ("skills", "languages"):
        names = f.value if isinstance(f.value, list) else [f.value]
        skill_ids = [sid for name in names if (sid := _resolve_skill_id(db, name)) is not None]
        if not skill_ids:
            return stmt.where(literal(False))
        skill_subq = select(EmployeeSkill.employee_id).where(EmployeeSkill.skill_id.in_(skill_ids))
        return stmt.where(Employee.id.in_(skill_subq))

    raise UnsupportedFilterError(f"'{f.field}' has no compiler rule yet")


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
        stmt = _apply_filter(db, stmt, f)

    if plan.order_by is not None:
        if plan.order_by not in _ORDERABLE_FIELDS:
            raise UnsupportedFilterError(f"order_by on '{plan.order_by}' is not compilable yet")
        stmt = stmt.order_by(getattr(Employee, plan.order_by))

    return stmt.limit(decision.max_rows)


def enforced_person_ref(db, caller: AuthenticatedUser, person_id: str) -> PersonRef | None:
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

    Builds the smallest possible PeopleQuery (select id+full_name, filter
    id eq person_id), enforces it, and returns None if policy denies it,
    the row is excluded by an obligation (restricted, non-hr caller), or
    the row doesn't exist -- same redact-never-reveal shape as everywhere
    else, never an error.
    """
    plan = PeopleQuery(select=["id", "full_name"], filters=[Filter(field="id", op="eq", value=person_id)])
    decision = enforce(plan, caller)
    if not decision.allow:
        return None
    row_id = db.execute(compile_query(db, plan, decision)).scalar()
    if row_id is None:
        return None
    person = db.get(Employee, row_id)
    if person is None:
        return None
    return PersonRef(id=person.id, full_name=person.full_name)
