"""The field registry: one source of truth for every field this API can
ever select, filter, or sort on, and how sensitive it is. ARCHITECTURE_2.md
§5. Everything downstream (app/query_plan.py's Field Literal, app/policy.py's
enforce()) reads from REGISTRY — there is no second list of field names
anywhere else that could drift out of sync with it.

Sensitivity tiers are a direct relabeling of app/permissions.py's existing
BASE_FIELDS/HR_ONLY_FIELDS, not a new judgment call about what's sensitive
— see the per-field comments below for the mapping. Deliberately only two
populated tiers (INTERNAL, HR_ONLY): every field gets a tier and goes
through the identical is_visible() check, including `id` and `full_name`
— there is no "public" tier that skips the check.

app/permissions.py's ABAC exception (personal_mobile: self or direct
manager) doesn't fit this per-role static model at all — "who comes back"
isn't known until the query runs, so it stays exactly where it already is,
applied post-retrieval. personal_mobile is registered here anyway (every
real column must be, per assert_registry_covers_schema below) but with
sensitivity=None: the static registry always denies it for select/filter/
order_by, and the only way it ever reaches a response is through
permissions.abac_extra_fields's existing post-retrieval grant, entirely
outside this system.

`direct_reports` (PersonSummary/OrgChainNode's downward-chain field,
manager+hr only) is deliberately NOT registered here. It's governed today
by an inline role check in app/people.py/app/org_chart.py, not by
permissions.py's BASE_FIELDS/HR_ONLY_FIELDS system — there's no existing
sensitivity tier to relabel for a "manager and hr, but not employee" field,
and inventing one is a real design decision, not a mechanical relabeling
pass. Flagged here as a known gap rather than silently folded into INTERNAL
(visible to employee too — wrong) or HR_ONLY (invisible to manager — also
wrong).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.engine import Engine


class Sensitivity(str, Enum):
    INTERNAL = "internal"      # app.permissions.BASE_FIELDS, plus id/full_name
    HR_ONLY = "hr_only"        # app.permissions.HR_ONLY_FIELDS
    DERIVED_HR = "derived_hr"  # unpopulated in Round 1 — Phase 4 adds
                                # continuity_exposure here once it exists;
                                # the label exists now so Phase 4 doesn't
                                # need to touch this file's shape.


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
    # today — Round 2 can widen this deliberately, not by silent default.
    "preferred_name": _f("preferred_name", "str", {"eq", "contains"}, Sensitivity.INTERNAL),
    "job_title": _f("job_title", "str", set(), Sensitivity.INTERNAL, filterable=False),
    "org_unit": _f("org_unit", "str", {"eq", "in"}, Sensitivity.INTERNAL, derived_from=("org_unit_id",)),
    "work_email": _f("work_email", "str", {"eq"}, Sensitivity.INTERNAL),
    "work_phone": _f("work_phone", "str", set(), Sensitivity.INTERNAL, filterable=False),
    "slack_handle": _f("slack_handle", "str", set(), Sensitivity.INTERNAL, filterable=False),
    "office": _f("office", "str", {"eq", "in", "contains"}, Sensitivity.INTERNAL, derived_from=("office_id",)),
    "effective_timezone": _f(
        "effective_timezone", "str", set(), Sensitivity.INTERNAL, filterable=False, derived_from=("timezone",)),
    "employment_type": _f("employment_type", "str", set(), Sensitivity.INTERNAL, filterable=False),
    "photo_url": _f("photo_url", "str", set(), Sensitivity.INTERNAL, filterable=False),
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

    # app.permissions.HR_ONLY_FIELDS (2 fields), relabeled HR_ONLY verbatim.
    "hire_date": _f("hire_date", "date", set(), Sensitivity.HR_ONLY, filterable=False),
    "cost_centre": _f("cost_centre", "str", set(), Sensitivity.HR_ONLY, filterable=False),

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

ALLOWED_SENSITIVITY: dict[str, frozenset[Sensitivity]] = {
    "employee": frozenset({Sensitivity.INTERNAL}),
    "manager": frozenset({Sensitivity.INTERNAL}),
    "hr": frozenset({Sensitivity.INTERNAL, Sensitivity.HR_ONLY, Sensitivity.DERIVED_HR}),
    # `manager` gets no extra *sensitivity* tier over `employee` — its
    # extra privileges (seeing direct_reports, e.g.) are row-scoping
    # obligations (app/policy.py), not broader field access.
}


def is_visible(field: str, role: str) -> bool:
    spec = REGISTRY.get(field)
    if spec is None:
        return False  # unlisted -> doesn't exist
    if spec.sensitivity is None:
        return False  # registered but unlabelled -> restricted until labelled
    return spec.sensitivity in ALLOWED_SENSITIVITY[role]


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
