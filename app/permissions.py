"""Field visibility as a config dictionary, not scattered if-statements.

RBAC sets the base field list per role. ABAC adds fields where the caller's
relationship to the target warrants it (e.g. their own profile, or a direct
report). A department check can further strip department-sensitive fields.
None of this touches the database — can_see_record() / visible_fields() are
pure functions over already-retrieved rows, applied strictly after retrieval.

Deny by default: a field absent from BASE_FIELDS for a role is never
returned, full stop, regardless of any other rule.
"""
from __future__ import annotations

from app.auth import AuthenticatedUser
from app.models import Employee, OrgUnit

# Visible to every caller who can see the record at all (record-level checks
# happen separately, in can_see_record()).
BASE_FIELDS: set[str] = {
    "preferred_name", "job_title", "org_unit", "work_email", "work_phone",
    "slack_handle", "office", "effective_timezone", "employment_type", "photo_url",
    "manager", "delegate", "availability_status", "away_until_month",
    "skills", "languages", "bio", "project_history", "tenure_band",
}

# HR-only, per the field table: "hire_date, cost_centre | hr only".
HR_ONLY_FIELDS: set[str] = {"hire_date", "cost_centre"}

# Training/certification status. Deliberately NOT in BASE_FIELDS: whether a
# colleague has finished their compliance training is nobody's business by
# default, the way a job title or an office is. Granted by RBAC to hr, and by
# ABAC to the person themself and to their reporting chain (see
# training_extra_fields below) — the same audience the notification triggers
# write to, so what management is told and what management can look up stay
# the same set of people.
TRAINING_FIELDS: set[str] = {"training_status"}

ALLOWED: dict[str, set[str]] = {
    "employee": set(BASE_FIELDS),
    "manager": set(BASE_FIELDS),
    "hr": set(BASE_FIELDS) | set(HR_ONLY_FIELDS) | set(TRAINING_FIELDS),
}

# Department-sensitive fields ("Engineering does not see Finance-sensitive
# fields") — currently just cost_centre, the one financial field in the
# schema. RBAC already restricts it to the hr role; HR is a company-wide
# function (not scoped to a division) so it's exempt from the division
# comparison below by design. This stage is a real, composable check —
# wired exactly where the pipeline calls for it — even though with today's
# 3-role setup it's a no-op on top of RBAC. It's what lets a future
# division-scoped role plug in without restructuring the pipeline.
DEPARTMENT_SENSITIVE_FIELDS: set[str] = {"cost_centre"}


def is_record_visible(caller: AuthenticatedUser, target: Employee) -> bool:
    """Record-level restriction: a small number of people are flagged
    'restricted' and don't appear at all, except to HR."""
    if target.availability_status.value == "restricted" and caller.role != "hr":
        return False
    return True


def division_of(db, org_unit_id: int | None) -> int | None:
    """Walk parent_id up from an org_unit to its division-level ancestor.

    Bounded to a handful of hops (the tree is company -> division ->
    department -> team, 4 levels deep) — no risk of the unbounded recursion
    the real org-chart traversal has to guard against in step 6.
    """
    unit = db.get(OrgUnit, org_unit_id) if org_unit_id is not None else None
    hops = 0
    while unit is not None and unit.unit_type != "division" and hops < 10:
        unit = db.get(OrgUnit, unit.parent_id) if unit.parent_id is not None else None
        hops += 1
    return unit.id if unit is not None else None


def department_filter(db, fields: set[str], caller: AuthenticatedUser, target: Employee) -> set[str]:
    """Strip department-sensitive fields the caller's own division doesn't warrant."""
    sensitive_present = fields & DEPARTMENT_SENSITIVE_FIELDS
    if not sensitive_present:
        return fields
    if caller.role == "hr":
        return fields  # HR operates company-wide, not division-scoped
    caller_division = division_of(db, _caller_org_unit_id(db, caller))
    target_division = division_of(db, target.org_unit_id)
    if caller_division is not None and caller_division == target_division:
        return fields
    return fields - DEPARTMENT_SENSITIVE_FIELDS


def _caller_org_unit_id(db, caller: AuthenticatedUser) -> int | None:
    caller_row = db.get(Employee, caller.id)
    return caller_row.org_unit_id if caller_row is not None else None


def abac_extra_fields(caller: AuthenticatedUser, target: Employee) -> set[str]:
    """personal_mobile: own profile, OR direct manager only."""
    if caller.id == target.id or target.manager_id == caller.id:
        return {"personal_mobile"}
    return set()


def training_extra_fields(db, caller: AuthenticatedUser, target: Employee) -> set[str]:
    """training_status: own profile, OR anywhere in the target's upward
    reporting chain.

    Not just the direct manager (unlike personal_mobile): a skip-level
    manager is told when their report's report resolves a course, so they
    can plainly already know — making the profile pretend otherwise would be
    theatre, not privacy. Peers and unrelated colleagues get nothing.

    The chain walk is the same traversal get_org_chain uses, minus the
    audit/RBAC wrapper (this IS the permission decision, so it can't depend
    on one). Only reached when the cheap checks above it fail, so the common
    case — viewing a stranger's profile — never runs a query.
    """
    if caller.id == target.id:
        return set(TRAINING_FIELDS)

    # Imported inside the function: org_chart imports people, which imports
    # this module, so a module-level import here would close the cycle. Same
    # local-import precedent as can_see_confidential_project below.
    from app.org_chart import manager_chain_ids

    # Full chain, independent of NOTIFY_LEVELS_UP: that setting controls who
    # gets *told*, and turning it down must not silently revoke a manager's
    # ability to *look*.
    if caller.id in manager_chain_ids(db, target.id):
        return set(TRAINING_FIELDS)
    return set()


def visible_fields(db, caller: AuthenticatedUser, target: Employee) -> set[str]:
    fields = set(ALLOWED[caller.role]) | abac_extra_fields(caller, target)
    if not TRAINING_FIELDS <= fields:  # hr already has it from RBAC — don't walk the chain for nothing
        fields |= training_extra_fields(db, caller, target)
    fields = department_filter(db, fields, caller, target)
    return fields


def can_see_confidential_project(db, caller: AuthenticatedUser, project_id: int) -> bool:
    """Confidential projects are visible to members only — no role or
    manager-relationship bypass, per spec ("A member's line manager gets no
    access unless they are a member themselves")."""
    from app.models import EmployeeProject

    return (
        db.query(EmployeeProject)
        .filter(EmployeeProject.employee_id == caller.id, EmployeeProject.project_id == project_id)
        .first()
        is not None
    )
