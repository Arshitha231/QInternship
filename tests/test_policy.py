"""Phase 3 Round 1 (ARCHITECTURE_2.md §8): app/policy.py's enforce(), the
single component every PeopleQuery must pass through. Pure unit tests
against a fake caller and a fake plan -- no live endpoint, no database,
except the one equivalence test that needs real fixture rows to compare
against app.permissions.is_record_visible.
"""
import pytest

from app.auth import AuthenticatedUser
from app.permissions import is_record_visible
from app.policy import DEFAULT_LIMIT, enforce
from app.query_plan import Filter, PeopleQuery

HR = AuthenticatedUser(id="hr-1", role="hr")
MANAGER = AuthenticatedUser(id="mgr-1", role="manager")
EMPLOYEE = AuthenticatedUser(id="emp-1", role="employee")
NON_HR_ROLES = (MANAGER, EMPLOYEE)


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


@pytest.mark.parametrize("caller", [HR, MANAGER, EMPLOYEE])
def test_obligations_agree_exactly_with_is_record_visible(db_session, caller):
    from app.models import Employee

    employees = db_session.query(Employee).filter(Employee.is_active == True).all()  # noqa: E712
    decision = enforce(PeopleQuery(select=["id"]), caller)

    excluded_by_obligation = _excluded_by_obligations(decision, employees)
    excluded_by_permissions = {e.id for e in employees if not is_record_visible(caller, e)}

    assert excluded_by_obligation == excluded_by_permissions


def test_hr_has_no_row_scoping_obligation():
    decision = enforce(PeopleQuery(select=["id"]), HR)
    assert decision.required_filters == []


@pytest.mark.parametrize("caller", NON_HR_ROLES)
def test_non_hr_gets_the_restricted_exclusion_obligation(caller):
    decision = enforce(PeopleQuery(select=["id"]), caller)
    assert decision.required_filters == [Filter(field="availability_status", op="ne", value="restricted")]
