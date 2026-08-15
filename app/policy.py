"""The policy engine (ARCHITECTURE_2.md §8): the single component every
PeopleQuery must pass through before anything it asks for reaches a
response. It does not return a boolean -- a PolicyDecision carries
redaction, required row-scoping filters (obligations), and the row cap,
none of which the caller or the model gets to negotiate.

Round 1: this is a pure function of (plan, caller, view_mode) with no
database access. Round 2 wired it into app.query_compiler.enforced_person_ref
-- the manager/delegate/direct_reports reference lookups inside
find_people/get_person/get_org_chain go through enforce() -> compile_query()
today. Round 3 makes it authoritative for those same three functions'
*primary* retrieval too: excluded_by_obligations() and compute_visible_fields()
below are what find_people/get_person/get_org_chain now consult instead of
app.permissions' is_record_visible/visible_fields directly. app.permissions
itself is untouched -- is_record_visible/visible_fields still exist and are
still the live gate for app.directory_tools (find_mentor/skill_gap/
skill_scarcity/find_project_owner) and app.notifications, a deliberately
deferred, separate scope. The ABAC layer (abac_extra_fields,
training_extra_fields, department_filter, can_see_confidential_project) was
never duplicated here and stays exactly where it is, called from
compute_visible_fields() the same way visible_fields() already calls it.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.auth import AuthenticatedUser
from app.models import Employee
from app.permissions import (
    TRAINING_FIELDS,
    ViewMode,
    abac_extra_fields,
    department_filter,
    effective_role,
    training_extra_fields,
)
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


def can_see_project_desc(role: str, view_mode: ViewMode = "work") -> bool:
    """project_desc (nested inside a project_history item) currently reaches
    a response via ALLOWED[("hr"/"it","work")]'s own construction in
    app.permissions -- REGISTRY has no entry for it at all, same known-gap
    category as direct_reports, since it's a Project-table field, not an
    employees column. compute_visible_fields() below needs an explicit
    check to stand in for what ALLOWED used to include implicitly."""
    return effective_role(role, view_mode) in ("hr", "it")


def can_see_training_status_by_role(role: str, view_mode: ViewMode = "work") -> bool:
    """training_status has two independent old-system grants, not one:
    app.permissions.training_extra_fields' self/manager-chain ABAC walk
    (still called by compute_visible_fields below, unchanged), AND hr's role
    getting it directly as part of ALLOWED[("hr","work")]'s own construction
    -- confirmed by this migration's own comparison tests catching its
    absence for an hr caller with no chain relationship to the target
    (test_new_stack_agrees_with_old_stack_for_every_employee). it does NOT
    get this grant in either system (ALLOWED[("it","work")] excludes
    TRAINING_FIELDS) -- it only ever sees training_status via the chain walk,
    same as employee/manager.
    """
    return effective_role(role, view_mode) == "hr"


def excluded_by_obligations(decision: PolicyDecision, employee: Employee) -> bool:
    """Evaluates a single already-fetched Employee against
    decision.required_filters directly -- no second query. This is the
    in-memory counterpart to compile_query()/query_compiler.apply_filter's
    query-level obligation application: used wherever an object is already
    in hand (get_person's target, get_org_chain's CTE-walked nodes) rather
    than wherever a query is still being built.

    Promoted from tests/test_policy.py's own _excluded_by_obligations() test
    helper, already indirectly proven correct against
    app.permissions.is_record_visible by
    test_obligations_agree_exactly_with_is_record_visible across all 8
    role/view_mode combinations -- not new, unverified logic.

    Raises on an obligation shape it doesn't recognize, deliberately, rather
    than silently letting it pass unenforced -- today there's exactly one
    shape (availability_status ne restricted); a second obligation type
    added to enforce() later must be taught here explicitly, not bypass this
    check by accident.
    """
    for f in decision.required_filters:
        if f.field != "availability_status" or f.op != "ne":
            raise ValueError(f"excluded_by_obligations does not know how to evaluate obligation {f!r}")
        if employee.availability_status.value == f.value:
            return True
    return False


_ALWAYS_PRESENT_FIELDS = frozenset({"id", "full_name"})


def compute_visible_fields(
    db, caller: AuthenticatedUser, target: Employee, view_mode: ViewMode = "work",
) -> set[str]:
    """Replaces app.permissions.visible_fields() for get_person/update_own_bio
    -- same pipeline shape (RBAC floor, then ABAC union, then training union,
    then department subtraction), just sourced from enforce() instead of
    ALLOWED for the RBAC floor. The ABAC layer itself (abac_extra_fields,
    training_extra_fields, department_filter) is untouched, imported from
    app.permissions exactly as visible_fields() already calls it -- that
    layer was never duplicated by REGISTRY and isn't being rebuilt here.

    id/full_name are deliberately excluded from the base plan even though
    REGISTRY labels them ordinary INTERNAL entries (registry.py's own
    docstring: "no field skips is_visible(), not even id/full_name") --
    visible_fields()'s actual contract never included them; _build_detail()
    sets both unconditionally, outside the `if "field" in fields` pattern
    every other field goes through. Including them here wouldn't change
    get_person's response body, but it WOULD change what get_person logs to
    AuditLog.fields_returned (app/people.py's fields_returned = fields is
    logged verbatim) -- a real, confirmed divergence caught by this
    migration's own comparison tests, not a cosmetic one.
    """
    plan_fields = [f for f in REGISTRY if f not in _ALWAYS_PRESENT_FIELDS]
    decision = enforce(PeopleQuery(select=plan_fields), caller, view_mode)
    fields = (set(plan_fields) - set(decision.dropped_fields)) | abac_extra_fields(caller, target)
    if can_see_training_status_by_role(caller.role, view_mode):
        fields |= TRAINING_FIELDS
    if not TRAINING_FIELDS <= fields:
        fields |= training_extra_fields(db, caller, target)
    if can_see_project_desc(caller.role, view_mode):
        fields.add("project_desc")
    return department_filter(db, fields, caller, target, view_mode)
