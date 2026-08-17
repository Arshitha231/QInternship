"""The field registry: one source of truth for every field this API can
ever select, filter, or sort on, and how sensitive it is. ARCHITECTURE_2.md
§5. Everything downstream (app/query_plan.py's Field Literal, app/policy.py's
enforce()) reads from REGISTRY — there is no second list of field names
anywhere else that could drift out of sync with it.

Sensitivity tiers are a direct relabeling of app/permissions.py's existing
BASE_FIELDS/INTERNAL_FIELDS (named HR_ONLY_FIELDS before view modes and the
"it" role were introduced there — same five fields, renamed on that side,
not re-derived on this one), not a new judgment call about what's sensitive
— see the per-field comments below for the mapping. Deliberately only two
populated tiers (INTERNAL, HR_ONLY): every field gets a tier and goes
through the identical is_visible() check, including `id` and `full_name`
— there is no "public" tier that skips the check.

app/permissions.py's ABAC exceptions don't fit this static (role, view_mode)
model at all — "who comes back" isn't known until the query runs, so they
stay exactly where they already are, applied post-retrieval, on top of
whatever this registry says statically:

  * personal_mobile (self or direct manager): registered anyway (every
    real column must be, per assert_registry_covers_schema below) but with
    sensitivity=None — the static registry always denies it for
    select/filter/order_by, and the only way it ever reaches a response is
    permissions.abac_extra_fields's post-retrieval grant, entirely outside
    this system.
  * salary / salary_currency / date_of_birth (self only, layered on a REAL
    HR_ONLY tier, not sensitivity=None): hr sees these statically, same as
    hire_date/cost_centre — that part IS a mechanical relabeling. The
    additional "the record's own subject can also see their own" grant is
    the same post-retrieval ABAC mechanism as personal_mobile, just added
    on top of a populated tier instead of an empty one. This registry only
    ever governs the RBAC floor; it was never the whole visibility story
    for any field ABAC touches.

`direct_reports` (PersonSummary/OrgChainNode's downward-chain field,
manager+hr only) and `training_status` (own profile, or anywhere in the
target's upward reporting chain, per permissions.training_extra_fields)
are deliberately NOT registered here. Both are governed by an inline
check elsewhere (app/people.py/app/org_chart.py; app/permissions.py),
not by a static per-role sensitivity tier — there's no existing tier to
relabel for "manager and hr, but not employee" or "self and your whole
reporting chain, but no one else," and inventing one is a real design
decision, not a mechanical relabeling pass. Flagged here as known gaps
rather than silently folded into INTERNAL (visible to employee too —
wrong for both) or HR_ONLY (invisible to manager/chain — also wrong for
both). Neither is a real `employees` column, so neither trips
assert_registry_covers_schema's drift check either.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.engine import Engine

from app.permissions import ViewMode


class Sensitivity(str, Enum):
    INTERNAL = "internal"      # app.permissions.BASE_FIELDS, plus id/full_name
    HR_ONLY = "hr_only"        # app.permissions.INTERNAL_FIELDS (nee HR_ONLY_FIELDS)
    DERIVED_HR = "derived_hr"  # deliberately left unpopulated, permanently for
                                # this cycle — NOT a placeholder waiting for
                                # Phase 4. Continuity (app/continuity.py) reads
                                # WorkAuthorizationRecord directly and is never
                                # joined into find_people/PeopleQuery at all, so
                                # keeping it out of REGISTRY entirely makes it
                                # structurally unreachable through this pipeline
                                # — stronger than labeling it DERIVED_HR and
                                # relying on enforce() to block it, since Field
                                # in app/query_plan.py is generated from
                                # REGISTRY.keys() and a labeled entry would be a
                                # legal PeopleQuery value for every role at the
                                # type level, gated only at runtime. It also has
                                # no coherent per-employee scalar shape to
                                # register: continuity classifies the client
                                # ENGAGEMENT, not the employee. Isolation is
                                # proven by tests/test_continuity_isolation.py
                                # instead — see that file and app/continuity.py.


FieldType = Literal["str", "int", "bool", "date", "list[str]"]


@dataclass(frozen=True)
class FieldSpec:
    name: str
    type: FieldType
    ops: frozenset[str]                 # legal Filter.op values for this field
    sensitivity: Sensitivity | None     # None => registered but unlabelled =>
                                         # is_visible() denies every role
    filterable: bool = True
    derived_from: tuple[str, ...] = ()  # source DB column(s), for fields that
                                         # aren't a 1:1 column (org_unit comes
                                         # from org_unit_id via a join, etc.)


def _f(
    name: str, type_: FieldType, ops: set[str], sensitivity: Sensitivity | None,
    *, filterable: bool = True, derived_from: tuple[str, ...] = (),
) -> FieldSpec:
    return FieldSpec(name=name, type=type_, ops=frozenset(ops), sensitivity=sensitivity,
                     filterable=filterable, derived_from=derived_from)


REGISTRY: dict[str, FieldSpec] = {
    # Always present, both INTERNAL — no field skips is_visible(), not even
    # the identifier fields.
    "id": _f("id", "str", {"eq", "in"}, Sensitivity.INTERNAL),
    "full_name": _f("full_name", "str", {"eq", "contains"}, Sensitivity.INTERNAL),

    # app.permissions.BASE_FIELDS (19 fields), relabeled INTERNAL verbatim.
    # filterable=False on fields find_people has no filter parameter for
    # today — widened deliberately, not by silent default, as each gets a
    # real caller. job_title's the first: Piece 2 (the model-emitted
    # PeopleQuery path) needs "title contains Architect", which nothing
    # could express before it had a REGISTRY entry at all. contains only --
    # an exact job_title match isn't a realistic ask, and the Search index
    # already had job_title as filterable, unused, waiting for this side to
    # catch up (search_index_schema.json).
    "preferred_name": _f("preferred_name", "str", {"eq", "contains"}, Sensitivity.INTERNAL),
    "job_title": _f("job_title", "str", {"contains"}, Sensitivity.INTERNAL),
    "org_unit": _f("org_unit", "str", {"eq", "in"}, Sensitivity.INTERNAL, derived_from=("org_unit_id",)),
    "work_email": _f("work_email", "str", {"eq"}, Sensitivity.INTERNAL),
    "work_phone": _f("work_phone", "str", set(), Sensitivity.INTERNAL, filterable=False),
    "slack_handle": _f("slack_handle", "str", set(), Sensitivity.INTERNAL, filterable=False),
    "office": _f("office", "str", {"eq", "in", "contains"}, Sensitivity.INTERNAL, derived_from=("office_id",)),
    "effective_timezone": _f(
        "effective_timezone", "str", set(), Sensitivity.INTERNAL, filterable=False, derived_from=("timezone",)),
    "employment_type": _f("employment_type", "str", set(), Sensitivity.INTERNAL, filterable=False),
    "photo_url": _f("photo_url", "str", set(), Sensitivity.INTERNAL, filterable=False),
    "linkedin_profile": _f(
        "linkedin_profile", "str", set(), Sensitivity.INTERNAL, filterable=False),
    "manager": _f("manager", "str", set(), Sensitivity.INTERNAL, filterable=False, derived_from=("manager_id",)),
    "delegate": _f("delegate", "str", set(), Sensitivity.INTERNAL, filterable=False, derived_from=("delegate_id",)),
    "availability_status": _f("availability_status", "str", {"eq", "ne", "in"}, Sensitivity.INTERNAL),
    "away_until_month": _f(
        "away_until_month", "str", set(), Sensitivity.INTERNAL, filterable=False, derived_from=("away_until",)),
    "skills": _f("skills", "list[str]", {"contains", "in"}, Sensitivity.INTERNAL),
    "languages": _f("languages", "list[str]", {"contains", "in"}, Sensitivity.INTERNAL),
    "bio": _f("bio", "str", set(), Sensitivity.INTERNAL, filterable=False),
    # Structured items (project name/role/dates), not really "list[str]" —
    # closest fit in FieldType's small taxonomy; moot anyway since it's not
    # filterable, so `type`/`ops` are never exercised for this one.
    "project_history": _f("project_history", "list[str]", set(), Sensitivity.INTERNAL, filterable=False),
    "tenure_band": _f(
        "tenure_band", "str", set(), Sensitivity.INTERNAL, filterable=False, derived_from=("hire_date",)),

    # app.permissions.INTERNAL_FIELDS (5 fields), relabeled HR_ONLY verbatim.
    # salary/salary_currency/date_of_birth additionally carry a self-only
    # ABAC grant (permissions.abac_extra_fields) on top of this HR_ONLY
    # floor -- see the module docstring; that grant is untouched by this
    # registry, same as personal_mobile's ABAC grant below is.
    "hire_date": _f("hire_date", "date", set(), Sensitivity.HR_ONLY, filterable=False),
    "cost_centre": _f("cost_centre", "str", set(), Sensitivity.HR_ONLY, filterable=False),
    "salary": _f("salary", "str", set(), Sensitivity.HR_ONLY, filterable=False),
    "salary_currency": _f("salary_currency", "str", set(), Sensitivity.HR_ONLY, filterable=False),
    "date_of_birth": _f("date_of_birth", "date", set(), Sensitivity.HR_ONLY, filterable=False),

    # ABAC-only — see module docstring. Unlabelled on purpose: the static
    # registry always denies it; permissions.abac_extra_fields grants it
    # post-retrieval, entirely outside this system.
    "personal_mobile": _f("personal_mobile", "str", set(), None, filterable=False),
}

# Real `employees` table columns with no registry entry, on purpose — FK
# columns that surface under a derived name instead, and internal columns
# never API-facing at all. Every entry needs a one-line reason; growing
# this set means touching test_registry.py's exact-contents test on
# purpose, not a silent side effect of adding a column.
IGNORED_COLUMNS: frozenset[str] = frozenset({
    "directory_object_id",  # Entra/SCIM external id — internal plumbing, never queried or displayed
    "is_active",             # soft-delete flag — already filtered at retrieval, never caller-facing
    "created_at",             # internal audit metadata, not API-facing
    "updated_at",             # internal audit metadata, not API-facing
    "timezone",               # superseded by the computed "effective_timezone" registry entry
    "away_until",             # superseded by the computed "away_until_month" registry entry
    "org_unit_id",            # FK; surfaces under the derived registry name "org_unit"
    "office_id",              # FK; surfaces under "office"
    "manager_id",             # FK; surfaces under "manager"
    "delegate_id",            # FK; surfaces under "delegate"
})

# Keyed by (role, view_mode), matching app.permissions.ALLOWED exactly --
# view modes didn't exist yet when this table was first built role-only, so
# an hr/it caller browsing in "employee" preview mode used to still get the
# full role-based set here even though every other part of the response
# correctly collapses to the employee view. Only `hr` actually differs
# between its two *reachable* modes (WORK_MODE_ROLES = {"hr", "it"} means
# employee/manager can never actually reach "work" regardless of what they
# request -- resolve_view_mode() pins them to "employee" first). The four
# rows below written out for both roles anyway, same as permissions.ALLOWED
# does, so every (role, view_mode) pair resolves without a KeyError -- two of
# them ("employee","work") / ("manager","work")) describe a state no real
# caller ever reaches, kept only so the table is total, not derived.
ALLOWED_SENSITIVITY: dict[tuple[str, ViewMode], frozenset[Sensitivity]] = {
    ("employee", "employee"): frozenset({Sensitivity.INTERNAL}),
    ("employee", "work"): frozenset({Sensitivity.INTERNAL}),  # unreachable, kept for totality
    # `manager` gets no extra *sensitivity* tier over `employee` in either
    # mode -- its extra privileges (seeing direct_reports, e.g.) are
    # row-scoping obligations (app/policy.py's can_see_direct_reports), not
    # broader field access.
    ("manager", "employee"): frozenset({Sensitivity.INTERNAL}),
    ("manager", "work"): frozenset({Sensitivity.INTERNAL}),  # unreachable, kept for totality
    # `hr` is the only role whose two *reachable* modes actually differ.
    ("hr", "employee"): frozenset({Sensitivity.INTERNAL}),
    ("hr", "work"): frozenset({Sensitivity.INTERNAL, Sensitivity.HR_ONLY, Sensitivity.DERIVED_HR}),
    # `it` (added alongside view modes in app/permissions.py, after this
    # registry was first built): same tier as employee/manager in both
    # modes, per that module's own ALLOWED table -- ("it", "work") gets
    # BASE_FIELDS plus project_desc, explicitly NOT INTERNAL_FIELDS ("it
    # administers the directory, it does not read salaries"). project_desc
    # itself has no REGISTRY entry (nested under project_history, not an
    # employees-table column) -- same known-gap category as
    # direct_reports/training_status, not something this tier needs to
    # account for.
    ("it", "employee"): frozenset({Sensitivity.INTERNAL}),
    ("it", "work"): frozenset({Sensitivity.INTERNAL}),
}


def is_visible(field: str, role: str, view_mode: ViewMode = "work") -> bool:
    spec = REGISTRY.get(field)
    if spec is None:
        return False  # unlisted -> doesn't exist
    if spec.sensitivity is None:
        return False  # registered but unlabelled -> restricted until labelled
    return spec.sensitivity in ALLOWED_SENSITIVITY[(role, view_mode)]


def assert_registry_covers_schema(engine: Engine, table_name: str = "employees") -> None:
    """Fail loudly at startup if a DB column has no registry entry and
    isn't explicitly ignored — this is what stops a teammate adding
    employee.home_address and having it silently queryable the same
    afternoon, one mechanism instead of one ad-hoc check per call site."""
    inspector = sa_inspect(engine)
    actual_columns = {col["name"] for col in inspector.get_columns(table_name)}
    covered = set(REGISTRY) | IGNORED_COLUMNS
    missing = actual_columns - covered
    if missing:
        raise RuntimeError(
            f"Column(s) in '{table_name}' with no registry entry and not in "
            f"IGNORED_COLUMNS: {sorted(missing)}. Add a REGISTRY entry "
            f"(app/registry.py) or an IGNORED_COLUMNS entry with a reason."
        )
