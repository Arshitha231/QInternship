"""The policy engine (ARCHITECTURE_2.md §8): the single component every
PeopleQuery must pass through before anything it asks for reaches a
response. It does not return a boolean -- a PolicyDecision carries
redaction, required row-scoping filters (obligations), and the row cap,
none of which the caller or the model gets to negotiate.

Round 1: this is a pure function of (plan, caller, view_mode) with no
database access. Round 2 wired it into app.query_compiler.enforced_person_ref
-- the manager/delegate/direct_reports reference lookups inside
find_people/get_person/get_org_chain go through enforce() -> compile_query()
today, but their own *primary* retrieval (the Search/SQL branches, the
recursive CTE) still runs on app.permissions' older is_record_visible/
visible_fields, which remains the live gate for everything else. That's a
deliberate, still-open scope boundary, not an oversight -- see this repo's
own notes on why a full cutover is a separate, larger decision.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.auth import AuthenticatedUser
from app.permissions import ViewMode, effective_role
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


def enforce(plan: PeopleQuery, caller: AuthenticatedUser, view_mode: ViewMode = "work") -> PolicyDecision:
    allowed = {name for name in REGISTRY if is_visible(name, caller.role, view_mode)}

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
    # employees are absent from everyone except hr); effective_role means
    # an hr caller browsing in employee view mode loses the exemption here
    # too, exactly like is_record_visible already does -- before view_mode
    # was threaded through, this checked raw caller.role, so an hr caller
    # in employee mode kept seeing restricted employees when nothing else
    # in the same response would have.
    #
    # No manager-scoped "manager_id = caller.id" obligation here, unlike
    # ARCHITECTURE_2.md §8's illustrative pseudocode -- direct_reports isn't
    # a PeopleQuery-shaped request at either of its two real call sites
    # (app/people.py's single-match enrichment, app/org_chart.py's
    # direction="down"), so there's no plan shape here for that obligation
    # to attach to. can_see_direct_reports() below is the actual row-scoping
    # decision those two call sites now share; inventing an unused
    # PeopleQuery obligation for it would be scope nothing consumes yet.
    required_filters: list[Filter] = []
    if effective_role(caller.role, view_mode) != "hr":
        required_filters.append(Filter(field="availability_status", op="ne", value="restricted"))

    return PolicyDecision(
        allow=True, reason="ok",
        required_filters=required_filters,
        dropped_fields=dropped_fields,
        max_rows=_max_rows(plan),
    )


def can_see_direct_reports(role: str, view_mode: ViewMode = "work") -> bool:
    """The one shared decision behind app.people's find_people enrichment and
    app.org_chart's get_org_chain(direction="down") gate -- previously two
    independently-written inline checks (`acting_role in ("manager", "hr")`
    vs `caller.role not in ("manager", "hr")`) that had already drifted:
    people.py's collapsed via effective_role for employee view mode,
    org_chart.py's didn't, since get_org_chain has no view_mode parameter at
    all. Lives here rather than app/registry.py because direct_reports isn't
    a field with a sensitivity tier -- registry.py's own ALLOWED_SENSITIVITY
    comment already frames this as a row-scoping obligation, not broader
    field access, which is exactly this module's job.
    """
    return effective_role(role, view_mode) in ("manager", "hr")
