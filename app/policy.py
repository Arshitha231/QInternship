"""The policy engine (ARCHITECTURE_2.md §8): the single component every
PeopleQuery must pass through before anything it asks for reaches a
response. It does not return a boolean -- a PolicyDecision carries
redaction, required row-scoping filters (obligations), and the row cap,
none of which the caller or the model gets to negotiate.

Round 1: this is a pure function of (plan, caller) with no database access
and no wiring into any live request path -- see the plan's own note on why
that split exists. Round 2 is what actually calls enforce() from
find_people/get_person/get_org_chain and compiles the result to SQL.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.auth import AuthenticatedUser
from app.query_plan import Filter, PeopleQuery
from app.registry import REGISTRY, is_visible

# Mirrors app.people.MAX_RESULTS -- not imported from there, so Round 1 adds
# zero new dependencies from/to any existing live-path module.
DEFAULT_LIMIT = 50

# Columns that are a genuine uniqueness constraint in the schema, not a
# coincidence of the seed data -- an exact match narrows to at most one
# person. full_name is deliberately excluded: two employees can legitimately
# share a name (the seeded "Priya Sharma" pair), so capping an exact
# full_name match at 1 would silently drop a real second match. There is no
# "ranked, top-k" case here at all (unlike ARCHITECTURE_2.md §8's simplified
# 3-row table) -- a PeopleQuery is structured filters only; free-text
# relevance ranking is Azure Search's own mechanism, entirely outside this
# schema (§16's "moving structured filters into Search... reverted by mode
# 1" cuts the same way in reverse: ranked retrieval never becomes a plan).
UNIQUE_EXACT_FIELDS = frozenset({"work_email", "slack_handle"})


@dataclass
class PolicyDecision:
    allow: bool
    reason: str  # audit only -- never surfaced to the caller, see below
    required_filters: list[Filter] = field(default_factory=list)
    dropped_fields: list[str] = field(default_factory=list)
    max_rows: int = DEFAULT_LIMIT


def _max_rows(plan: PeopleQuery) -> int:
    for f in plan.filters:
        if f.op == "eq" and f.field in UNIQUE_EXACT_FIELDS:
            return 1
    limit = DEFAULT_LIMIT
    if plan.limit is not None:
        # Model may hint a smaller number, never a larger one.
        limit = min(limit, max(1, plan.limit))
    return limit


def enforce(plan: PeopleQuery, caller: AuthenticatedUser) -> PolicyDecision:
    allowed = {name for name in REGISTRY if is_visible(name, caller.role)}

    # 1. Redact, don't reject -- select fields the role can't see are
    # silently dropped. The caller finds out by absence, never by error.
    dropped_fields = [f for f in plan.select if f not in allowed]

    # 2. INVARIANT 6: a filter on a field the role can't see is not
    # redacted, it's a hard denial -- WHERE cost_centre = 'X' leaks
    # membership through which rows come back even with the column never
    # selected.
    illegal_filter = next((f for f in plan.filters if f.field not in allowed), None)
    if illegal_filter is not None:
        return PolicyDecision(allow=False, reason=f"filter on restricted field '{illegal_filter.field}'")

    # 2b. Same invariant applies to sorting -- ORDER BY a hidden field
    # leaks its grouping the same way, without projecting the column.
    if plan.order_by is not None and plan.order_by not in allowed:
        return PolicyDecision(allow=False, reason=f"order_by on restricted field '{plan.order_by}'")

    # 3. Obligations -- row scoping the model never sees and cannot
    # negotiate. This is the query-plan-level expression of
    # app.permissions.is_record_visible's existing rule (restricted
    # employees are absent from everyone except hr); Round 1 pins that
    # equivalence with a direct test (tests/test_policy.py), Round 2
    # decides where this is actually enforced live.
    #
    # No manager-scoped obligation here (e.g. "manager_id = caller.id" for
    # a direct-reports request), unlike ARCHITECTURE_2.md §8's illustrative
    # pseudocode -- direct_reports isn't a registry field at all yet
    # (app/registry.py's own docstring flags this as a known gap owned by
    # app/people.py's inline role check), so there's no plan shape here for
    # that obligation to attach to. Adding one would be inventing scope
    # this pass doesn't cover, not implementing something already decided.
    required_filters: list[Filter] = []
    if caller.role != "hr":
        required_filters.append(Filter(field="availability_status", op="ne", value="restricted"))

    return PolicyDecision(
        allow=True, reason="ok",
        required_filters=required_filters,
        dropped_fields=dropped_fields,
        max_rows=_max_rows(plan),
    )
