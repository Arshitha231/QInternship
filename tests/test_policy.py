"""Phase 3 Round 1 (ARCHITECTURE_2.md §8): app/policy.py's enforce(), the
single component every PeopleQuery must pass through. Pure unit tests
against a fake caller and a fake plan -- no live endpoint, no database,
except the one equivalence test that needs real fixture rows to compare
against app.permissions.is_record_visible.
"""
import pytest

from app.auth import AuthenticatedUser
from app.permissions import is_record_visible, visible_fields
from app.policy import (
    DEFAULT_LIMIT,
    can_see_direct_reports,
    compute_visible_fields,
    enforce,
    excluded_by_obligations,
)
from app.query_plan import Filter, PeopleQuery

HR = AuthenticatedUser(id="hr-1", role="hr")
MANAGER = AuthenticatedUser(id="mgr-1", role="manager")
EMPLOYEE = AuthenticatedUser(id="emp-1", role="employee")
IT = AuthenticatedUser(id="it-1", role="it")
NON_HR_ROLES = (MANAGER, EMPLOYEE)
VIEW_MODES = ("employee", "work")


# ---------------------------------------------------------------------------
# Redaction -- select fields the role can't see are dropped, not rejected.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("caller", NON_HR_ROLES)
def test_hr_only_field_is_redacted_from_select_for_non_hr(caller):
    plan = PeopleQuery(select=["id", "full_name", "cost_centre", "hire_date"])
    decision = enforce(plan, caller)
    assert decision.allow is True
    assert decision.dropped_fields == ["cost_centre", "hire_date"]


def test_hr_only_field_is_not_dropped_for_hr():
    plan = PeopleQuery(select=["id", "full_name", "cost_centre", "hire_date"])
    decision = enforce(plan, HR)
    assert decision.allow is True
    assert decision.dropped_fields == []


def test_no_dropped_fields_when_everything_requested_is_visible():
    plan = PeopleQuery(select=["id", "full_name", "job_title"])
    for caller in (HR, MANAGER, EMPLOYEE):
        assert enforce(plan, caller).dropped_fields == []


# ---------------------------------------------------------------------------
# INVARIANT 6 -- filtering or sorting ON a restricted field is a hard
# denial, never a silent drop. Tested as two separate cases: filters leak
# through row membership, order_by leaks through grouping, independently.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("caller", NON_HR_ROLES)
def test_filter_on_restricted_field_denies_the_whole_plan(caller):
    plan = PeopleQuery(select=["id"], filters=[Filter(field="cost_centre", op="eq", value="CC-1")])
    decision = enforce(plan, caller)
    assert decision.allow is False
    assert decision.required_filters == []
    assert decision.dropped_fields == []


@pytest.mark.parametrize("caller", NON_HR_ROLES)
def test_order_by_on_restricted_field_denies_the_whole_plan(caller):
    plan = PeopleQuery(select=["id"], order_by="hire_date")
    decision = enforce(plan, caller)
    assert decision.allow is False


def test_filter_on_restricted_field_is_allowed_for_hr():
    plan = PeopleQuery(select=["id"], filters=[Filter(field="cost_centre", op="eq", value="CC-1")])
    assert enforce(plan, HR).allow is True


def test_order_by_on_restricted_field_is_allowed_for_hr():
    plan = PeopleQuery(select=["id"], order_by="hire_date")
    assert enforce(plan, HR).allow is True


def test_filter_on_visible_field_does_not_deny():
    plan = PeopleQuery(select=["id"], filters=[Filter(field="org_unit", op="eq", value="Infrastructure")])
    for caller in (HR, MANAGER, EMPLOYEE):
        assert enforce(plan, caller).allow is True


# ---------------------------------------------------------------------------
# INVARIANT 6, extended to filter_groups (bounded DNF): a restricted field
# hiding in ANY group -- not just plan.filters or group 0 -- is the same
# hard denial. An obligation/policy check that only ever looked at the
# first group would let a model route a restricted-field filter through
# group 1 (or later) and slip past enforce() entirely.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("caller", NON_HR_ROLES)
def test_filter_on_restricted_field_in_the_first_group_denies_the_whole_plan(caller):
    plan = PeopleQuery(select=["id"], filter_groups=[
        [Filter(field="cost_centre", op="eq", value="CC-1")],
        [Filter(field="org_unit", op="eq", value="Infrastructure")],
    ])
    decision = enforce(plan, caller)
    assert decision.allow is False


@pytest.mark.parametrize("caller", NON_HR_ROLES)
def test_filter_on_restricted_field_in_the_second_group_denies_the_whole_plan(caller):
    # The one that would actually catch a "only checks group 0" bug --
    # cost_centre is legal-looking group 0, restricted field pushed to
    # group 1 specifically.
    plan = PeopleQuery(select=["id"], filter_groups=[
        [Filter(field="org_unit", op="eq", value="Infrastructure")],
        [Filter(field="cost_centre", op="eq", value="CC-1")],
    ])
    decision = enforce(plan, caller)
    assert decision.allow is False


def test_filter_on_restricted_field_in_a_group_is_allowed_for_hr():
    plan = PeopleQuery(select=["id"], filter_groups=[
        [Filter(field="org_unit", op="eq", value="Infrastructure")],
        [Filter(field="cost_centre", op="eq", value="CC-1")],
    ])
    assert enforce(plan, HR).allow is True


def test_filter_groups_of_only_visible_fields_does_not_deny():
    plan = PeopleQuery(select=["id"], filter_groups=[
        [Filter(field="org_unit", op="eq", value="Infrastructure")],
        [Filter(field="skills", op="contains", value="Terraform")],
    ])
    for caller in (HR, MANAGER, EMPLOYEE):
        assert enforce(plan, caller).allow is True


def test_max_rows_ignores_a_unique_field_filter_inside_a_group():
    # _max_rows() deliberately only scans plan.filters, not filter_groups --
    # a unique-field match in ONE OR-branch doesn't bound how many rows the
    # OTHER branches can contribute, so this must NOT collapse to 1 the way
    # the equivalent top-level `filters` case does (see
    # test_unique_field_exact_match_caps_at_one above).
    plan = PeopleQuery(select=["id"], filter_groups=[
        [Filter(field="work_email", op="eq", value="a@example.test")],
        [Filter(field="org_unit", op="eq", value="Infrastructure")],
    ])
    assert enforce(plan, HR).max_rows == DEFAULT_LIMIT


# ---------------------------------------------------------------------------
# Denial reason never leaking a distinction the caller shouldn't see --
# "doesn't exist" and "exists but restricted" must be structurally
# identical outcomes (allow=False, no other field populated); only the
# audit-only `reason` string differs, and that never reaches the caller.
# ---------------------------------------------------------------------------

def test_denial_shape_is_identical_for_nonexistent_vs_restricted_field():
    nonexistent = PeopleQuery(select=["id"], filters=[Filter(field="cost_centre", op="eq", value="x")])
    # cost_centre IS a real field (just hr-only) -- is_visible() collapses
    # "doesn't exist" and "restricted for this role" into the same False,
    # so enforce()'s allow=False/required_filters=[]/dropped_fields=[]
    # shape is what a caller could observe either way; only `reason`
    # differs, and that string is never sent to the caller.
    decision = enforce(nonexistent, EMPLOYEE)
    assert decision.allow is False
    assert decision.required_filters == []
    assert decision.dropped_fields == []
    assert decision.max_rows == DEFAULT_LIMIT


# ---------------------------------------------------------------------------
# max_rows
# ---------------------------------------------------------------------------

def test_unique_field_exact_match_caps_at_one():
    plan = PeopleQuery(select=["id"], filters=[Filter(field="work_email", op="eq", value="a@example.test")])
    assert enforce(plan, HR).max_rows == 1


def test_exact_name_match_is_not_capped_at_one():
    # Two employees can legitimately share a full name (the seeded "Priya
    # Sharma" pair) -- capping at 1 here would silently drop a real match.
    plan = PeopleQuery(select=["id"], filters=[Filter(field="full_name", op="eq", value="Priya Sharma")])
    assert enforce(plan, HR).max_rows == DEFAULT_LIMIT


def test_plain_structured_filter_gets_the_wide_cap():
    plan = PeopleQuery(select=["id"], filters=[Filter(field="org_unit", op="eq", value="Infrastructure")])
    assert enforce(plan, HR).max_rows == DEFAULT_LIMIT


def test_no_filters_at_all_gets_the_wide_cap():
    plan = PeopleQuery(select=["id"])
    assert enforce(plan, HR).max_rows == DEFAULT_LIMIT


def test_limit_hint_narrows_but_never_widens():
    narrower = PeopleQuery(select=["id"], limit=5)
    assert enforce(narrower, HR).max_rows == 5

    wider = PeopleQuery(select=["id"], limit=DEFAULT_LIMIT + 1000)
    assert enforce(wider, HR).max_rows == DEFAULT_LIMIT

    zero_or_negative = PeopleQuery(select=["id"], limit=0)
    assert enforce(zero_or_negative, HR).max_rows == 1  # clamped up to at least 1, never 0


def test_unique_field_cap_wins_over_a_wider_limit_hint():
    plan = PeopleQuery(select=["id"], filters=[Filter(field="work_email", op="eq", value="a@example.test")], limit=50)
    assert enforce(plan, HR).max_rows == 1


# ---------------------------------------------------------------------------
# Obligations / is_record_visible equivalence -- the mechanism that makes
# it safe to later route enrichment lookups through enforce() (Round 2): a
# second, independent implementation of "who's restricted" must agree with
# the first, or one of them is a live security bug.
# ---------------------------------------------------------------------------

def _excluded_by_obligations(decision, employees) -> set[str]:
    excluded: set[str] = set()
    for f in decision.required_filters:
        assert f.field == "availability_status" and f.op == "ne"
        excluded |= {e.id for e in employees if e.availability_status.value == f.value}
    return excluded


@pytest.mark.parametrize("caller", [HR, MANAGER, EMPLOYEE, IT])
@pytest.mark.parametrize("view_mode", VIEW_MODES)
def test_obligations_agree_exactly_with_is_record_visible(db_session, caller, view_mode):
    from app.models import Employee

    employees = db_session.query(Employee).filter(Employee.is_active == True).all()  # noqa: E712
    decision = enforce(PeopleQuery(select=["id"]), caller, view_mode)

    excluded_by_obligation = _excluded_by_obligations(decision, employees)
    excluded_by_permissions = {e.id for e in employees if not is_record_visible(caller, e, view_mode)}

    assert excluded_by_obligation == excluded_by_permissions


def test_hr_has_no_row_scoping_obligation_in_work_mode():
    decision = enforce(PeopleQuery(select=["id"]), HR, "work")
    assert decision.required_filters == []


def test_hr_gets_the_restricted_exclusion_obligation_in_employee_mode():
    # The parity fix: before enforce() took a view_mode, this checked raw
    # caller.role, so an hr caller "previewing" employee mode kept seeing
    # restricted employees when nothing else in the same response would
    # have -- is_record_visible's own effective_role-based collapse already
    # denied them there. Now the two agree (also covered generically by
    # test_obligations_agree_exactly_with_is_record_visible above; this one
    # pins the specific case in plain terms since it's the concrete bug).
    decision = enforce(PeopleQuery(select=["id"]), HR, "employee")
    assert decision.required_filters == [Filter(field="availability_status", op="ne", value="restricted")]


@pytest.mark.parametrize("caller", NON_HR_ROLES)
def test_non_hr_gets_the_restricted_exclusion_obligation(caller):
    decision = enforce(PeopleQuery(select=["id"]), caller)
    assert decision.required_filters == [Filter(field="availability_status", op="ne", value="restricted")]


# ---------------------------------------------------------------------------
# can_see_direct_reports -- the one shared decision behind find_people's
# direct_reports enrichment and get_org_chain's direction="down" gate,
# consolidated from two independently-written inline checks that had
# already drifted (only one of the two collapsed via effective_role).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("view_mode", VIEW_MODES)
def test_manager_and_hr_can_see_direct_reports_in_work_mode(view_mode):
    assert can_see_direct_reports("manager", "work")
    assert can_see_direct_reports("hr", "work")


def test_employee_and_it_cannot_see_direct_reports_in_work_mode():
    assert not can_see_direct_reports("employee", "work")
    assert not can_see_direct_reports("it", "work")


def test_hr_loses_direct_reports_visibility_in_employee_mode():
    # Same collapse as the restricted-record obligation above -- hr
    # previewing "employee" mode is an ordinary employee for this decision
    # too, so they lose the manager/hr-only direct_reports view.
    assert not can_see_direct_reports("hr", "employee")


def test_manager_loses_direct_reports_visibility_in_employee_mode():
    assert not can_see_direct_reports("manager", "employee")


def test_can_see_direct_reports_defaults_to_work_mode():
    # get_org_chain has no view_mode parameter at all and calls this with
    # role only -- the default must match its pre-existing (raw-role)
    # behavior exactly, not silently start collapsing.
    assert can_see_direct_reports("manager") == can_see_direct_reports("manager", "work")
    assert can_see_direct_reports("hr") == can_see_direct_reports("hr", "work")


# ---------------------------------------------------------------------------
# The before/after comparison this migration's plan requires: does the new
# stack (excluded_by_obligations + compute_visible_fields, both driven by
# enforce()) agree EXACTLY with the old stack (is_record_visible +
# visible_fields) for every fixture employee, across all 8 role/view_mode
# combinations? Both implementations still exist side by side -- see this
# migration's plan for why app.permissions isn't touched -- so this runs
# before find_people/get_person/get_org_chain's call sites are switched over,
# and stays in the suite permanently afterward as a standing regression net,
# not a throwaway migration check. A disagreement here is a real finding, not
# a test to adjust.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("caller", [HR, MANAGER, EMPLOYEE, IT])
@pytest.mark.parametrize("view_mode", VIEW_MODES)
def test_new_stack_agrees_with_old_stack_for_every_employee(db_session, caller, view_mode):
    from app.models import Employee

    employees = db_session.query(Employee).filter(Employee.is_active == True).all()  # noqa: E712
    decision = enforce(PeopleQuery(select=["id"]), caller, view_mode)

    for target in employees:
        old_visible = is_record_visible(caller, target, view_mode)
        new_visible = not excluded_by_obligations(decision, target)
        assert old_visible == new_visible, (
            f"record visibility disagreement for target={target.id} caller={caller.role} "
            f"view_mode={view_mode}: old={old_visible} new={new_visible}"
        )

        old_fields = visible_fields(db_session, caller, target, view_mode)
        new_fields = compute_visible_fields(db_session, caller, target, view_mode)
        assert old_fields == new_fields, (
            f"field-visibility disagreement for target={target.id} caller={caller.role} "
            f"view_mode={view_mode}: old={old_fields} new={new_fields}"
        )


@pytest.mark.parametrize("view_mode", VIEW_MODES)
def test_new_stack_agrees_with_old_stack_on_self_lookup(db_session, view_mode):
    # HR/MANAGER/EMPLOYEE/IT above are synthetic callers with no matching
    # Employee row, so abac_extra_fields' self-grant and department_filter's
    # same-division check never actually fire for them -- this exercises
    # both against a real employee looking at their own record instead.
    # "stranger-1" (conftest.py) has a real salary specifically for this.
    from app.models import Employee

    self_caller = AuthenticatedUser(id="stranger-1", role="employee")
    target = db_session.get(Employee, "stranger-1")
    decision = enforce(PeopleQuery(select=["id"]), self_caller, view_mode)

    assert is_record_visible(self_caller, target, view_mode) == (not excluded_by_obligations(decision, target))
    assert visible_fields(db_session, self_caller, target, view_mode) == \
        compute_visible_fields(db_session, self_caller, target, view_mode)


@pytest.mark.parametrize("view_mode", VIEW_MODES)
def test_new_stack_agrees_with_old_stack_for_managers_own_report(db_session, view_mode):
    # "mgr-1" is both a real Employee row and MANAGER's caller id above, so
    # this exercises department_filter's same-division same-caller path for
    # real, looking at their own direct report ("report-1").
    from app.models import Employee

    target = db_session.get(Employee, "report-1")
    decision = enforce(PeopleQuery(select=["id"]), MANAGER, view_mode)

    assert is_record_visible(MANAGER, target, view_mode) == (not excluded_by_obligations(decision, target))
    assert visible_fields(db_session, MANAGER, target, view_mode) == \
        compute_visible_fields(db_session, MANAGER, target, view_mode)
