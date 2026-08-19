"""Phase 3 Round 2 (ARCHITECTURE_2.md §11): app/query_compiler.py, the one
place a PeopleQuery becomes SQL. Exercised against the real conftest.py
fixture data (tests/conftest.py's `_database`), not a synthetic stand-in --
office="Test HQ"/Testville, "Satellite Office"/"Satellite City", org units
"Platform Engineering" (eng_dept) / "Finance Operations" (fin_dept),
"Rory Restricted" (availability_status=restricted), "Taylor Cloud" /
"Jordan Ledger" (Terraform), "Jordan Ledger" also speaks French.
"""
import pytest

from app.auth import AuthenticatedUser
from app.models import Employee
from app.policy import PolicyDecision, enforce
from app.query_compiler import UnsupportedFilterError, compile_query, enforced_person_ref
from app.query_plan import Filter, PeopleQuery

HR = AuthenticatedUser(id="qc-hr", role="hr")
EMPLOYEE = AuthenticatedUser(id="qc-emp", role="employee")


def _run(db_session, plan: PeopleQuery, caller=HR) -> list[str]:
    decision = enforce(plan, caller)
    assert decision.allow
    return db_session.execute(compile_query(db_session, plan, decision)).scalars().all()


# ---------------------------------------------------------------------------
# Direct-column filters
# ---------------------------------------------------------------------------

def test_id_eq_returns_exactly_that_row(db_session):
    result = _run(db_session, PeopleQuery(select=["id"], filters=[Filter(field="id", op="eq", value="report-1")]))
    assert result == ["report-1"]


def test_full_name_eq_is_case_insensitive(db_session):
    result = _run(db_session, PeopleQuery(
        select=["id"], filters=[Filter(field="full_name", op="eq", value="riley report")]))
    assert result == ["report-1"]


def test_work_email_eq_returns_exactly_that_row(db_session):
    result = _run(db_session, PeopleQuery(
        select=["id"], filters=[Filter(field="work_email", op="eq", value="riley@example.test")]))
    assert result == ["report-1"]


def test_full_name_contains(db_session):
    result = _run(db_session, PeopleQuery(
        select=["id"], filters=[Filter(field="full_name", op="contains", value="Restricted")]))
    assert result == ["restricted-1"]


def test_availability_status_ne(db_session):
    result = _run(db_session, PeopleQuery(
        select=["id"], filters=[Filter(field="availability_status", op="ne", value="restricted")], limit=1000))
    assert "restricted-1" not in result


def test_id_in_list(db_session):
    result = set(_run(db_session, PeopleQuery(
        select=["id"], filters=[Filter(field="id", op="in", value=["report-1", "mgr-1"])])))
    assert result == {"report-1", "mgr-1"}


# ---------------------------------------------------------------------------
# Derived/joined filters
# ---------------------------------------------------------------------------

def test_org_unit_filters_to_that_department_only(db_session):
    result = set(_run(db_session, PeopleQuery(
        select=["id"], filters=[Filter(field="org_unit", op="eq", value="Finance Operations")], limit=1000)))
    assert "stranger-1" in result
    assert "search-filter-fin" in result
    assert "mgr-1" not in result  # eng_dept, not fin_dept


def test_office_filters_by_city(db_session):
    result = set(_run(db_session, PeopleQuery(
        select=["id"], filters=[Filter(field="office", op="eq", value="Satellite City")], limit=1000)))
    assert result == {"search-filter-fin"}


def test_skills_contains_one_name(db_session):
    result = set(_run(db_session, PeopleQuery(
        select=["id"], filters=[Filter(field="skills", op="contains", value="Terraform")], limit=1000)))
    # Not a closed exact-set: tests/test_proposed_changes.py's
    # test_accepted_skill_lands_as_learning_self_reported legitimately
    # commits a real (Learning, self-reported) Terraform skill for the
    # shared fixture employee "extract-alex" via the accept-a-proposal
    # workflow -- that's the behavior under test there, not a leak to clean
    # up. Asserting the two original holders are still present (never
    # silently dropped) plus that nobody UNEXPECTED shows up still catches
    # a real regression in either direction.
    assert {"search-filter-eng", "search-filter-fin"} <= result <= {
        "search-filter-eng", "search-filter-fin", "extract-alex"}


def test_languages_contains(db_session):
    result = set(_run(db_session, PeopleQuery(
        select=["id"], filters=[Filter(field="languages", op="contains", value="French")], limit=1000)))
    assert result == {"search-filter-fin"}


def test_skills_in_list_matches_any(db_session):
    result = set(_run(db_session, PeopleQuery(
        select=["id"],
        filters=[Filter(field="skills", op="in", value=["Terraform", "Power BI"])], limit=1000)))
    # See test_skills_contains_one_name: "extract-alex" may or may not
    # legitimately hold Terraform depending on whether
    # test_proposed_changes.py's accept-workflow test has run yet in this
    # session -- same tolerance, same reasoning.
    assert {"search-filter-eng", "search-filter-fin", "search-dana"} <= result <= {
        "search-filter-eng", "search-filter-fin", "search-dana", "extract-alex"}


def test_unresolvable_skill_name_returns_nothing_not_an_error(db_session):
    result = _run(db_session, PeopleQuery(
        select=["id"], filters=[Filter(field="skills", op="contains", value="Zzyzx Nonexistent")]))
    assert result == []


def test_unresolvable_org_unit_returns_nothing_not_an_error(db_session):
    result = _run(db_session, PeopleQuery(
        select=["id"], filters=[Filter(field="org_unit", op="eq", value="Zzyzx Nonexistent")]))
    assert result == []


# ---------------------------------------------------------------------------
# Ordering, capping, obligations, and error paths
# ---------------------------------------------------------------------------

def test_order_by_full_name_is_alphabetical(db_session):
    result = _run(db_session, PeopleQuery(
        select=["id"],
        filters=[Filter(field="id", op="in", value=["report-1", "mgr-1", "restricted-1"])],
        order_by="full_name", limit=1000,
    ))
    # Morgan Manager, Riley Report, Rory Restricted -- alphabetical by full_name
    assert result == ["mgr-1", "report-1", "restricted-1"]


def test_max_rows_from_policy_decision_actually_caps_the_query(db_session):
    plan = PeopleQuery(select=["id"], limit=3)
    decision = enforce(plan, HR)
    assert decision.max_rows == 3
    result = db_session.execute(compile_query(db_session, plan, decision)).scalars().all()
    assert len(result) == 3


def test_obligation_excludes_restricted_for_non_hr(db_session):
    plan = PeopleQuery(select=["id"], filters=[Filter(field="id", op="eq", value="restricted-1")])
    decision = enforce(plan, EMPLOYEE)
    result = db_session.execute(compile_query(db_session, plan, decision)).scalars().all()
    assert result == []  # obligation applied even though the plan's own filter asked for exactly this id


def test_obligation_does_not_exclude_restricted_for_hr(db_session):
    plan = PeopleQuery(select=["id"], filters=[Filter(field="id", op="eq", value="restricted-1")])
    decision = enforce(plan, HR)
    result = db_session.execute(compile_query(db_session, plan, decision)).scalars().all()
    assert result == ["restricted-1"]


def test_compile_query_refuses_a_denied_decision(db_session):
    plan = PeopleQuery(select=["id"], filters=[Filter(field="cost_centre", op="eq", value="x")])
    decision = enforce(plan, EMPLOYEE)
    assert decision.allow is False
    with pytest.raises(ValueError):
        compile_query(db_session, plan, decision)


def test_non_filterable_field_raises_unsupported_not_silently_wrong(db_session):
    # Bypasses validate() on purpose -- a plan that reached compile_query()
    # without going through the validator first should fail loudly, not
    # silently compile something that ignores the (illegal) filter.
    # work_phone (not job_title -- see the next test) stays a clean example
    # of a field with no filterable ops at all.
    plan = PeopleQuery(select=["id"], filters=[Filter(field="work_phone", op="eq", value="x")])
    decision = PolicyDecision(allow=True, reason="test-only, bypassing validate()")
    with pytest.raises(UnsupportedFilterError):
        compile_query(db_session, plan, decision)


def test_job_title_filterable_but_only_for_contains(db_session):
    # job_title is filterable (Piece 2), but its only legal op is "contains"
    # -- "eq" bypassing validate() should still raise, same
    # loud-not-silent shape as a field with no ops at all, for a different
    # reason (illegal op for this field, not "not filterable").
    plan = PeopleQuery(select=["id"], filters=[Filter(field="job_title", op="eq", value="x")])
    decision = PolicyDecision(allow=True, reason="test-only, bypassing validate()")
    with pytest.raises(UnsupportedFilterError):
        compile_query(db_session, plan, decision)

    contains_plan = PeopleQuery(select=["id"], filters=[Filter(field="job_title", op="contains", value="Architect")])
    contains_decision = PolicyDecision(allow=True, reason="test-only")
    result = db_session.execute(compile_query(db_session, contains_plan, contains_decision)).scalars().all()
    assert isinstance(result, list)  # compiles and executes without raising


def test_order_by_on_a_derived_field_raises_unsupported(db_session):
    plan = PeopleQuery(select=["id"], order_by="org_unit")
    decision = enforce(plan, HR)
    with pytest.raises(UnsupportedFilterError):
        compile_query(db_session, plan, decision)


def test_only_active_employees_are_ever_returned(db_session):
    # No inactive fixture rows exist to flip, so this pins the WHERE clause
    # itself rather than data -- every compiled statement must carry
    # Employee.is_active == True regardless of what filters were asked for.
    from sqlalchemy import inspect

    plan = PeopleQuery(select=["id"])
    decision = enforce(plan, HR)
    stmt = compile_query(db_session, plan, decision)
    compiled_sql = str(stmt.compile(compile_kwargs={"literal_binds": False}))
    assert "is_active" in compiled_sql


# ---------------------------------------------------------------------------
# filter_groups -- bounded DNF (app/query_plan.py's PeopleQuery docstring):
# outer OR across groups, inner AND within a group, one level, no recursion.
# ---------------------------------------------------------------------------

def test_filter_groups_ors_two_disjoint_single_id_groups(db_session):
    # Simplest possible proof the union actually implements OR: two groups
    # that could never both match the same row, same result shape as
    # test_id_in_list's `op="in"` case but built from filter_groups instead.
    result = set(_run(db_session, PeopleQuery(
        select=["id"],
        filter_groups=[
            [Filter(field="id", op="eq", value="report-1")],
            [Filter(field="id", op="eq", value="mgr-1")],
        ],
        limit=1000,
    )))
    assert result == {"report-1", "mgr-1"}


def test_filter_groups_ors_across_different_fields(db_session):
    # The actual "cross-field OR" case filters/op=in cannot express: office
    # and skills are different fields, and apply_filter's office branch
    # does its own .join(Office, ...) -- proving that join stays scoped to
    # its own group's Select and doesn't collide with the other group's
    # skills subquery.
    result = set(_run(db_session, PeopleQuery(
        select=["id"],
        filter_groups=[
            [Filter(field="office", op="eq", value="Satellite City")],
            [Filter(field="skills", op="contains", value="Power BI")],
        ],
        limit=1000,
    )))
    assert "search-filter-fin" in result  # Satellite City, first group
    assert "search-dana" in result        # Power BI, second group


def test_filter_groups_alone_with_no_top_level_filters(db_session):
    # plan.filters entirely empty -- filter_groups must stand on its own,
    # not silently require a top-level filter to combine with.
    result = set(_run(db_session, PeopleQuery(
        select=["id"],
        filters=[],
        filter_groups=[[Filter(field="id", op="eq", value="report-1")]],
    )))
    assert result == {"report-1"}


def test_filters_and_filter_groups_compose_by_and(db_session):
    # filters is an unconditional AND constraint layered UNDER the OR:
    # restricted-1 matches the second group by id exactly, but fails the
    # top-level availability_status filter, so it must not appear even
    # though "one of its groups" says yes.
    result = _run(db_session, PeopleQuery(
        select=["id"],
        filters=[Filter(field="availability_status", op="eq", value="available")],
        filter_groups=[
            [Filter(field="id", op="eq", value="report-1")],
            [Filter(field="id", op="eq", value="restricted-1")],
        ],
        limit=1000,
    ))
    assert result == ["report-1"]


def test_filter_groups_single_group_behaves_like_plain_filters(db_session):
    # len(filter_groups) == 1 is a degenerate OR-of-one -- must not error on
    # sqlalchemy.union()'s "needs at least 2 selects" shape, and must return
    # exactly what the equivalent plain `filters` plan would.
    via_group = set(_run(db_session, PeopleQuery(
        select=["id"], filter_groups=[[Filter(field="org_unit", op="eq", value="Finance Operations")]], limit=1000)))
    via_filters = set(_run(db_session, PeopleQuery(
        select=["id"], filters=[Filter(field="org_unit", op="eq", value="Finance Operations")], limit=1000)))
    assert via_group == via_filters


def test_filter_groups_obligation_excludes_restricted_even_from_the_matching_group(db_session):
    # The non-negotiable: a restricted row must not slip through because
    # it happens to match a DIFFERENT OR-branch than the one obligations
    # were checked against. restricted-1 matches the second group by id
    # exactly; for a non-hr caller it must still be excluded.
    plan = PeopleQuery(
        select=["id"],
        filter_groups=[
            [Filter(field="org_unit", op="eq", value="Finance Operations")],
            [Filter(field="id", op="eq", value="restricted-1")],
        ],
    )
    employee_decision = enforce(plan, EMPLOYEE)
    employee_result = db_session.execute(compile_query(db_session, plan, employee_decision)).scalars().all()
    assert "restricted-1" not in employee_result
    assert "search-filter-fin" in employee_result  # first group's own match is untouched

    hr_decision = enforce(plan, HR)
    hr_result = db_session.execute(compile_query(db_session, plan, hr_decision)).scalars().all()
    assert "restricted-1" in hr_result  # hr has no such obligation -- not spuriously excluded either


def test_filter_groups_rejects_a_non_filterable_field_in_the_second_group(db_session):
    # Mirrors test_non_filterable_field_raises_unsupported_not_silently_wrong
    # above, but the illegal field is in the SECOND group -- proving
    # _compile_group_ids doesn't only get exercised for group 0.
    plan = PeopleQuery(select=["id"], filter_groups=[
        [Filter(field="id", op="eq", value="report-1")],
        [Filter(field="work_phone", op="eq", value="x")],
    ])
    decision = PolicyDecision(allow=True, reason="test-only, bypassing validate()")
    with pytest.raises(UnsupportedFilterError):
        compile_query(db_session, plan, decision)


# ---------------------------------------------------------------------------
# enforced_person_ref() -- view_mode threading. Regression test for the
# concrete bug this closes: before view_mode was threaded through, this
# always evaluated as if the caller were in full "work" mode, so an hr
# caller previewing "employee" mode would still see a restricted manager's
# name via a manager/delegate reference (find_people/get_person attach
# whichever person a manager_id/delegate_id points at through this exact
# function) even though every other field on the same response was
# correctly anonymized to the employee view. Exercised directly against
# "restricted-1" (Rory Restricted, availability_status=restricted, conftest.py)
# as the looked-up person -- enforced_person_ref doesn't care who's asking
# about whom, only which person_id it's resolving, so this is equivalent to
# and simpler than going through a manager_id indirection.
# ---------------------------------------------------------------------------

def test_enforced_person_ref_hides_a_restricted_manager_in_employee_mode(db_session):
    ref = enforced_person_ref(db_session, HR, "restricted-1", "employee")
    assert ref is None


def test_enforced_person_ref_shows_a_restricted_manager_in_work_mode(db_session):
    # Unchanged behavior -- hr in full work mode still sees restricted
    # records, same as is_record_visible everywhere else.
    ref = enforced_person_ref(db_session, HR, "restricted-1", "work")
    assert ref is not None
    assert ref.id == "restricted-1"


def test_enforced_person_ref_default_argument_matches_explicit_work_mode(db_session):
    # get_org_chain calls this with no view_mode argument at all -- the
    # implicit default must be identical to explicitly passing "work",
    # not a silent behavior change for the one call site with nothing else
    # to give it.
    default_ref = enforced_person_ref(db_session, HR, "restricted-1")
    explicit_ref = enforced_person_ref(db_session, HR, "restricted-1", "work")
    assert default_ref == explicit_ref


def test_full_name_supports_in_for_a_chain_second_step(db_session):
    """A chain resolves a set in step 1 and filters by it in step 2. The
    model picks whichever identifier step 1's result made obvious -- ids or
    names -- and only ids were permitted, so golden eval t3-14 was rejected
    mid-chain with "operator 'in' not legal for field 'full_name'"."""
    result = _run(db_session, PeopleQuery(
        select=["id"],
        filters=[Filter(field="full_name", op="in", value=["Riley Report", "Morgan Manager"])]))
    assert set(result) == {"report-1", "mgr-1"}
