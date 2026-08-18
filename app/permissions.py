"""Field visibility as a config dictionary, not scattered if-statements.

RBAC sets the base field list per (role, view_mode). ABAC adds fields where
the caller's relationship to the target warrants it (e.g. their own profile,
or a direct report). A department check can further strip
department-sensitive fields. None of this touches the database —
is_record_visible() / visible_fields() are pure functions over
already-retrieved rows, applied strictly after retrieval.

Deny by default: a field absent from ALLOWED for a (role, view_mode) pair is
never returned, full stop, regardless of any other rule.

VIEW MODES
----------
Every role sees the directory through one of two lenses:

    work      — the caller's full privileges. What every role got before
                view modes existed, so "work" is the behaviour-preserving
                mode and the default for internal callers.
    employee  — the ordinary-colleague view. Output is identical no matter
                who is looking, so an hr or it caller can see exactly what
                the directory looks like to the people in it.

Only hr and it may choose; resolve_view_mode() forces employee mode for
everyone else regardless of what the client asked for. Employee mode is
enforced in three places, not one — the field table below, the record-level
check, and the department check — because each is a separate stage of the
pipeline and any one of them left role-aware would leak a privileged
caller's identity back into a view that is supposed to be anonymous.
"""
from __future__ import annotations

from typing import Literal

from app.auth import AuthenticatedUser
from app.models import Employee, OrgUnit

ViewMode = Literal["employee", "work"]
VALID_VIEW_MODES: set[str] = {"employee", "work"}

# Roles allowed to ask for work mode. Everyone else is pinned to employee
# mode server-side — the client's request is an input to this decision, never
# the decision itself.
WORK_MODE_ROLES: set[str] = {"hr", "it"}

# Visible to every caller who can see the record at all (record-level checks
# happen separately, in can_see_record()).
#
# project_desc is NOT listed here even though it's now visible to everyone
# (see PROJECT_DESC_FIELDS below and app/people.py's _project_history) — it's
# nested inside project_history's list items, not an employees-table column,
# same "known gap" category app/registry.py documents for direct_reports and
# training_status: those two are also universally-or-conditionally visible
# without a BASE_FIELDS/REGISTRY entry, checked inline where they're built
# instead. test_registry.py asserts every BASE_FIELDS/INTERNAL_FIELDS member
# has a REGISTRY entry — a real column-completeness check — so adding a
# nested, non-column field here would be fighting that invariant for a field
# it was never meant to cover, not satisfying it.
BASE_FIELDS: set[str] = {
    "preferred_name", "job_title", "org_unit", "work_email", "work_phone",
    "slack_handle", "office", "effective_timezone", "employment_type", "photo_url",
    "manager", "delegate", "availability_status", "away_until_month",
    "skills", "languages", "bio", "project_history", "tenure_band",
    "linkedin_profile", "name_pronunciation",
}

# Internal HR information. Granted to exactly one (role, view_mode) pair —
# ("hr", "work") — and to nobody else, including it in work mode: running the
# directory's systems is not a reason to read what people are paid.
#
# Still additionally granted to the person themselves by ABAC below, in
# either mode. INTERNAL here means "no other ROLE gets these", not "only HR
# ever sees them" — an employee looking at their own salary is not HR
# information leaking, it is their own record.
INTERNAL_FIELDS: set[str] = {
    "hire_date", "cost_centre", "salary", "salary_currency", "date_of_birth",
}

# Project descriptions. Visible to hr and it in work mode; it is additionally
# Visible to every caller, unconditionally — app/people.py's
# _project_history includes project_desc on every item it builds, with no
# per-caller check at all (see the note in BASE_FIELDS above for why this
# isn't expressed as a fields-set membership test the way most fields are).
# Still referenced here because EDITABLE needs to name exactly this one
# field: visibility is universal now, editing stayed IT-only.
PROJECT_DESC_FIELDS: set[str] = {"project_desc"}

# Own profile only. Deliberately narrower than personal_mobile's "own profile
# OR direct manager": a line manager holding your mobile number is ordinary,
# a line manager reading your salary and date of birth off the directory is
# not. Managers get neither, at any level of the chain — unlike
# training_status, which the chain can see precisely because the chain is
# already told about it.
SELF_ONLY_FIELDS: set[str] = {"salary", "salary_currency", "date_of_birth"}

# Training/certification status. Deliberately NOT in BASE_FIELDS: whether a
# colleague has finished their compliance training is nobody's business by
# default, the way a job title or an office is. Granted by RBAC to hr, and by
# ABAC to the person themself and to their reporting chain (see
# training_extra_fields below) — the same audience the notification triggers
# write to, so what management is told and what management can look up stay
# the same set of people.
TRAINING_FIELDS: set[str] = {"training_status"}

# The field table, keyed by (role, view_mode).
#
# Written out in full — all four roles x both modes — rather than derived,
# so that "what does an it caller see in work mode?" is answered by reading
# one line instead of simulating a collapse rule. The four employee-mode
# rows are deliberately the identical BASE_FIELDS set: employee-mode output
# being the same for every role is a property of this table by
# construction, not something a downstream function has to remember to
# enforce. test_employee_mode_table_is_role_independent asserts it here, and
# the HTTP-level test asserts it again end to end.
ALLOWED: dict[tuple[str, str], set[str]] = {
    ("employee", "employee"): set(BASE_FIELDS),
    ("manager", "employee"): set(BASE_FIELDS),
    ("hr", "employee"): set(BASE_FIELDS),
    ("it", "employee"): set(BASE_FIELDS),

    # employee/manager can never reach work mode — resolve_view_mode() pins
    # them to employee mode. Their rows are present so the table answers the
    # (field, role, view_mode) question for every triple rather than raising
    # a KeyError on one that policy says is unreachable, and they are
    # identical to their employee-mode rows so that if the pin were ever
    # loosened by accident, the failure mode is "no change", not "escalation".
    ("employee", "work"): set(BASE_FIELDS),
    ("manager", "work"): set(BASE_FIELDS),

    ("hr", "work"): set(BASE_FIELDS) | set(INTERNAL_FIELDS) | set(TRAINING_FIELDS),
    # No INTERNAL_FIELDS: it administers the directory, it does not read
    # salaries. This is the asymmetry the whole (role, view_mode) re-key
    # exists to express — before it, "hr-only" and "privileged" were the
    # same thing. project_desc is no longer listed here specifically: it's
    # in BASE_FIELDS now, so every row already carries it.
    ("it", "work"): set(BASE_FIELDS),
}

# Write permissions, same key shape as ALLOWED and for the same reason:
# the endpoint asks this table, it does not re-derive the rule. Enforced in
# the service layer so that every caller of a write path — HTTP route,
# tool-calling layer, future batch job — hits the same gate.
EDITABLE: dict[tuple[str, str], set[str]] = {
    ("employee", "employee"): set(),
    ("manager", "employee"): set(),
    ("hr", "employee"): set(),
    ("it", "employee"): set(),
    ("employee", "work"): set(),
    ("manager", "work"): set(),

    # HR edits internal information on any employee. availability_status is
    # how a profile gets marked "restricted" (see is_record_visible below —
    # that enforcement has always existed; this is what was missing to
    # actually set it). manager_id and the two synthetic capability names
    # ("deactivate_employee", "create_employee") aren't literal fields a
    # generic PATCH touches directly the way the others are — same pattern
    # ("it", "work")'s "skills"/"contribution"/"project_entry" already uses
    # below, a capability this table gates without it being a raw column
    # name on the model.
    ("hr", "work"): {
        "full_name", "preferred_name", "job_title", "work_email", "work_phone",
        "salary", "salary_currency", "date_of_birth", "hire_date", "cost_centre",
        "employment_type", "linkedin_profile", "availability_status", "manager_id",
        "deactivate_employee", "create_employee", "restrict_employee",
    },
    # IT's write surface: project descriptions (PUT/DELETE
    # /projects/{id}/description) plus, as of the doc-review pipeline, the
    # three field kinds that pipeline can propose — "skills" gates writing
    # an EmployeeSkill row, "contribution" gates EmployeeProject.contribution
    # (the prose), "project_entry" gates the EmployeeProject membership
    # itself (which project, what role, when). Three separate names, not one
    # umbrella "review_pipeline" field, so a future narrowing (e.g. IT loses
    # skill-writing but keeps project descriptions) is a one-line change here
    # instead of a new carve-out. app.proposals.accept()/bulk_accept() check
    # this per change_type, the same can_edit() every other write path uses
    # — there is no separate, hardcoded "role == it" gate anywhere in the
    # review pipeline; this table is the only place that decision is made.
    ("it", "work"): set(PROJECT_DESC_FIELDS) | {"skills", "contribution", "project_entry"},
}

# Department-sensitive fields ("Engineering does not see Finance-sensitive
# fields") — currently just cost_centre, the one financial field in the
# schema. RBAC already restricts it to the hr role; HR is a company-wide
# function (not scoped to a division) so it's exempt from the division
# comparison below by design. This stage is a real, composable check —
# wired exactly where the pipeline calls for it — even though with today's
# 3-role setup it's a no-op on top of RBAC. It's what lets a future
# division-scoped role plug in without restructuring the pipeline.
DEPARTMENT_SENSITIVE_FIELDS: set[str] = {"cost_centre", "salary", "salary_currency"}
# salary belongs here as the most obviously finance-sensitive field in the
# schema. It changes nothing today — HR is exempt as a company-wide function,
# and the only other holder is the person themselves, who is by definition in
# their own division — but leaving the pay field out of the department check
# would be the wrong thing to find here later. date_of_birth is deliberately
# absent: it's personal data, not financial, and this stage is the money one.


def resolve_view_mode(role: str, requested: str | None) -> ViewMode:
    """The server's decision about which lens a request is answered through.

    The client's `view_mode` parameter is an input here and nowhere else. A
    caller whose role isn't in WORK_MODE_ROLES gets employee mode however
    they ask — including an unrecognised value, which is pinned rather than
    rejected so that a malformed parameter can only ever narrow what comes
    back, never widen it.

    hr/it default to work mode when they don't ask, because work mode is
    what those roles got before view modes existed and a silent narrowing
    would look like data loss on their existing profile pages.
    """
    if role not in WORK_MODE_ROLES:
        return "employee"
    if requested not in VALID_VIEW_MODES:
        return "work"
    return requested  # type: ignore[return-value]


def effective_role(role: str, view_mode: ViewMode) -> str:
    """The role the pipeline reasons with, as opposed to the one the caller
    holds.

    In employee mode every caller is treated as an ordinary employee. The
    field table already encodes this for field-level filtering; this is what
    carries the same collapse into the two stages that are keyed on the role
    directly rather than through the table — the restricted-record check and
    the department check.
    """
    return "employee" if view_mode == "employee" else role


def is_record_visible(
    caller: AuthenticatedUser, target: Employee, view_mode: ViewMode = "work"
) -> bool:
    """Record-level restriction: a small number of people are flagged
    'restricted' and don't appear at all, except to HR.

    In employee mode HR loses that exemption, so a restricted record 404s
    for them exactly as it does for everyone else. That is the point of the
    mode — an HR caller who still saw restricted people while claiming to
    look at the ordinary employee view would be reading a view nobody else
    has, and the identity guarantee would be false.

    Defaults to work mode so every existing caller (org_chart,
    directory_tools, notifications) keeps its current behaviour without
    having to opt in; the read endpoints pass the resolved mode explicitly.
    """
    if target.availability_status.value == "restricted" and effective_role(caller.role, view_mode) != "hr":
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


def department_filter(
    db, fields: set[str], caller: AuthenticatedUser, target: Employee,
    view_mode: ViewMode = "work",
) -> set[str]:
    """Strip department-sensitive fields the caller's own division doesn't warrant."""
    sensitive_present = fields & DEPARTMENT_SENSITIVE_FIELDS
    if not sensitive_present:
        return fields
    if effective_role(caller.role, view_mode) == "hr":
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
    """personal_mobile: own profile, OR direct manager.
    salary / salary_currency / date_of_birth: own profile ONLY."""
    fields: set[str] = set()
    if caller.id == target.id:
        fields |= {"personal_mobile"} | set(SELF_ONLY_FIELDS)
    elif target.manager_id == caller.id:
        fields |= {"personal_mobile"}
    return fields


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


def visible_fields(
    db, caller: AuthenticatedUser, target: Employee, view_mode: ViewMode = "work"
) -> set[str]:
    """The full pipeline: RBAC table -> ABAC -> training chain -> department.

    ABAC and the training-chain walk are deliberately NOT collapsed in
    employee mode. Both key on the caller's identity and relationships
    (caller.id == target.id, target.manager_id, the reporting chain), never
    on their role — so they return the same answer for a given pair of
    people whoever is asking, which is exactly what the identity guarantee
    requires. Stripping them in employee mode would make an hr caller's own
    profile show less than their colleague's does, which isn't a truer
    employee view, just a broken one.
    """
    fields = set(ALLOWED[(caller.role, view_mode)]) | abac_extra_fields(caller, target)
    if not TRAINING_FIELDS <= fields:  # hr already has it from RBAC — don't walk the chain for nothing
        fields |= training_extra_fields(db, caller, target)
    fields = department_filter(db, fields, caller, target, view_mode)
    return fields


def can_edit(role: str, view_mode: ViewMode, field: str) -> bool:
    """Write-side counterpart to visible_fields, same table shape.

    Called by the service layer on every write, so a request that never
    performed the read the UI would have done still hits the same gate. The
    UI hiding an edit control is a convenience; this is the enforcement.
    """
    return field in EDITABLE.get((role, view_mode), set())


def editable_fields(role: str, view_mode: ViewMode) -> set[str]:
    return set(EDITABLE.get((role, view_mode), set()))


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
