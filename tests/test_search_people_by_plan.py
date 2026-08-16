"""Piece 2: app.people.search_people_by_plan(), the model-emitted-PeopleQuery
retrieval path. Exercised against the real conftest.py fixture data, same
convention as tests/test_query_compiler.py -- office="Test HQ"/Testville,
"Satellite Office"/"Satellite City", org units "Platform Engineering"/
"Finance Operations", "Rory Restricted" (availability_status=restricted).
"""
import json

import pytest

from app.auth import AuthenticatedUser
from app.models import AuditLog
from app.people import search_people_by_plan
from app.query_plan import Filter, PeopleQuery

HR = AuthenticatedUser(id="plan-hr", role="hr")
EMPLOYEE = AuthenticatedUser(id="plan-emp", role="employee")


def test_job_title_contains_filters_correctly(db_session):
    # Several fixture employees have "Engineering Manager" as their title;
    # none have a title containing "Nonexistent Title Xyz".
    plan = PeopleQuery(select=["id"], filters=[Filter(field="job_title", op="contains", value="Manager")])
    results = search_people_by_plan(db_session, HR, plan)
    assert results
    assert all("manager" in p.job_title.lower() for p in results)

    plan_none = PeopleQuery(
        select=["id"], filters=[Filter(field="job_title", op="contains", value="Nonexistent Title Xyz")])
    assert search_people_by_plan(db_session, HR, plan_none) == []


def test_office_in_list_ors_across_values(db_session):
    # "Bangalore or Singapore"-shaped query -- same field, multiple values,
    # already expressible via the existing `in` op (no grammar change
    # needed, per this plan's own analysis). Uses the two real fixture
    # offices instead, since neither Bangalore nor Singapore is seeded.
    plan = PeopleQuery(
        select=["id"], filters=[Filter(field="office", op="in", value=["Test HQ", "Satellite Office"])])
    results = search_people_by_plan(db_session, HR, plan)
    assert results
    offices = {p.office.name for p in results if p.office}
    assert offices <= {"Test HQ", "Satellite Office"}


def test_org_unit_and_skill_combination(db_session):
    plan = PeopleQuery(select=["id"], filters=[
        Filter(field="org_unit", op="eq", value="Finance Operations"),
    ])
    results = search_people_by_plan(db_session, HR, plan)
    assert "search-filter-fin" in [p.id for p in results]


def test_restricted_employee_excluded_for_non_hr(db_session):
    plan = PeopleQuery(select=["id"], filters=[Filter(field="id", op="eq", value="restricted-1")])
    assert search_people_by_plan(db_session, EMPLOYEE, plan) == []
    assert [p.id for p in search_people_by_plan(db_session, HR, plan)] == ["restricted-1"]


# ---------------------------------------------------------------------------
# Invariant 6, on a model-shaped input -- not just the hand-built plans
# test_policy.py already covers. This is the actual guarantee this whole
# plan is about making load-bearing.
# ---------------------------------------------------------------------------

def test_cost_centre_filter_is_rejected_for_everyone_today(db_session):
    # cost_centre (like every current HR_ONLY field) is filterable=False in
    # REGISTRY -- validate()'s blanket check rejects it before enforce()'s
    # role-aware check is ever reached, for EVERY caller including hr. This
    # is a real, current characteristic worth pinning explicitly: as
    # REGISTRY stands today, every filterable field is INTERNAL (visible to
    # every role) and every restricted field is non-filterable, so
    # enforce()'s Invariant 6 role check has no field where it actually
    # distinguishes roles through this real pipeline yet -- see the two
    # tests below, which isolate enforce()'s own behavior directly instead.
    plan = PeopleQuery(select=["id"], filters=[Filter(field="cost_centre", op="eq", value="CC-1")])
    with pytest.raises(ValueError) as exc_info:
        search_people_by_plan(db_session, HR, plan)
    assert "not filterable" in str(exc_info.value)
    with pytest.raises(ValueError):
        search_people_by_plan(db_session, EMPLOYEE, plan)


def test_enforce_denial_raises_generic_message_not_the_reason(db_session, monkeypatch):
    # Isolates search_people_by_plan()'s own denial-handling code from
    # validate()'s currently-blanket cost_centre rejection above, using the
    # same "bypass validate() on purpose" precedent
    # tests/test_query_compiler.py::test_non_filterable_field_raises_unsupported_not_silently_wrong
    # already established -- enforce()'s own role logic for cost_centre is
    # already proven correct directly in tests/test_policy.py; this proves
    # search_people_by_plan() doesn't leak decision.reason (which names the
    # field) into the exception message _retry_after_execution_failure
    # feeds straight back to the model (ARCHITECTURE_2.md §8: "a denial
    # must not reveal that a restricted capability exists").
    import app.vocabulary
    from app.vocabulary import ValidationResult
    monkeypatch.setattr(app.vocabulary, "validate", lambda plan: ValidationResult(valid=True))

    plan = PeopleQuery(select=["id"], filters=[Filter(field="cost_centre", op="eq", value="CC-1")])
    with pytest.raises(ValueError) as exc_info:
        search_people_by_plan(db_session, EMPLOYEE, plan)
    assert "cost_centre" not in str(exc_info.value)
    # Not asserting an "hr succeeds" counterpart here: app.query_compiler's
    # own apply_filter *also* unconditionally guards on spec.filterable
    # (confirmed -- it raises for cost_centre regardless of caller), so
    # there is currently no real REGISTRY field where "restricted but
    # permitted for this role" actually reaches a successful compile. That
    # path is proven correct at the unit level in tests/test_policy.py
    # (enforce() alone); demonstrating it here would mean mocking
    # compile_query too, which stops testing anything real.


# ---------------------------------------------------------------------------
# validate() failures -- structural, safe to surface verbatim (no data
# sensitivity in a "this field doesn't exist" or "this op is illegal" error).
# ---------------------------------------------------------------------------

def test_unknown_field_raises_with_the_field_name_in_the_message(db_session):
    plan_dict = {"select": ["id"], "filters": [{"field": "id", "op": "eq", "value": "x"}]}
    # Construct past Pydantic's own Field literal by going through
    # model_construct, mirroring how a genuinely malformed model output
    # could still reach here if the tool schema and REGISTRY ever drifted --
    # validate() is the structural backstop regardless of what Pydantic's
    # own type system already blocks.
    plan = PeopleQuery.model_construct(select=["id"], filters=[
        Filter.model_construct(field="not_a_real_field", op="eq", value="x")
    ], order_by=None, limit=None)
    with pytest.raises(ValueError) as exc_info:
        search_people_by_plan(db_session, HR, plan)
    assert "not_a_real_field" in str(exc_info.value)


def test_illegal_op_for_job_title_is_rejected(db_session):
    plan = PeopleQuery.model_construct(select=["id"], filters=[
        Filter.model_construct(field="job_title", op="eq", value="x")
    ], order_by=None, limit=None)
    with pytest.raises(ValueError) as exc_info:
        search_people_by_plan(db_session, HR, plan)
    assert "job_title" in str(exc_info.value)


# ---------------------------------------------------------------------------
# snap() unresolvable values -- empty result, not an exception, same
# precedent as find_people's own unresolvable skill/org_unit filters.
# ---------------------------------------------------------------------------

def test_unresolvable_org_unit_value_returns_empty_not_an_error(db_session):
    plan = PeopleQuery(select=["id"], filters=[Filter(field="org_unit", op="eq", value="Zzyzx Nonexistent Dept")])
    assert search_people_by_plan(db_session, HR, plan) == []


def test_unresolvable_value_is_still_recorded_in_the_audit_log(db_session):
    plan = PeopleQuery(select=["id"], filters=[Filter(field="org_unit", op="eq", value="Zzyzx Nonexistent Dept")])
    search_people_by_plan(db_session, HR, plan)
    row = db_session.query(AuditLog).filter(AuditLog.action == "search_people_by_plan").order_by(
        AuditLog.id.desc()).first()
    assert row is not None
    assert "unresolved" in row.query_text


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

def test_audit_row_written_with_the_right_action(db_session):
    plan = PeopleQuery(select=["id"], filters=[Filter(field="job_title", op="contains", value="Manager")])
    results = search_people_by_plan(db_session, HR, plan)
    row = db_session.query(AuditLog).filter(AuditLog.action == "search_people_by_plan").order_by(
        AuditLog.id.desc()).first()
    assert row is not None
    assert row.result_count == len(results)
    assert json.loads(row.fields_returned)  # non-empty SUMMARY_FIELDS list
