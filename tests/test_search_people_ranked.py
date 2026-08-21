"""app.people.search_people_ranked() -- SEARCH_RANKING_IMPLEMENTATION_PLAN.md
step 4's wiring into the retrieval pipeline. Detailed scoring arithmetic is
tests/test_people_ranking.py's job; this file is about the wiring itself:
the same validate -> snap -> enforce -> compile_query pipeline
search_people_by_plan uses (via the shared _compile_plan_ids helper), with
the same permission boundary, reordered by score instead of by
compile_query's own order. Exercised against the real conftest.py fixture
data, same convention as tests/test_search_people_by_plan.py -- skill
"Terraform" (search-filter-eng holds it at Expert, search-filter-fin at
Learning), "Rory Restricted" (availability_status=restricted, job_title
"Legal Counsel").
"""
import json

from app.auth import AuthenticatedUser
from app.models import AuditLog
from app.people import search_people_ranked
from app.query_entities import Entity, Interpretation
from app.query_plan import Filter, PeopleQuery

HR = AuthenticatedUser(id="ranked-hr", role="hr")
EMPLOYEE = AuthenticatedUser(id="ranked-emp", role="employee")


def _entity(label: str, value: str) -> Entity:
    return Entity(label=label, span=(0, len(value)), text=value, value=value, confidence=1.0)


def test_a_higher_scoring_candidate_is_returned_first(db_session):
    """search-filter-eng holds Terraform at Expert, search-filter-fin at
    Learning -- the same single-skill query must rank Expert first, not in
    whatever order compile_query's own id-select happened to return."""
    plan = PeopleQuery(select=["id"], filters=[Filter(field="skills", op="contains", value="Terraform")])
    interp = Interpretation(entities=[_entity("skill", "Terraform")], unparsed=[])
    results, any_holds_all = search_people_ranked(db_session, HR, plan, interp)
    ids = [p.id for p in results]
    assert "search-filter-eng" in ids and "search-filter-fin" in ids
    assert ids.index("search-filter-eng") < ids.index("search-filter-fin")
    assert any_holds_all is True  # a single requested skill -- both hold it


def test_restricted_employee_excluded_for_non_hr(db_session):
    plan = PeopleQuery(select=["id"], filters=[Filter(field="job_title", op="contains", value="Legal")])
    interp = Interpretation(entities=[_entity("role", "Legal Counsel")], unparsed=[])

    results, _ = search_people_ranked(db_session, EMPLOYEE, plan, interp)
    assert results == []

    results_hr, _ = search_people_ranked(db_session, HR, plan, interp)
    assert [p.id for p in results_hr] == ["restricted-1"]


def test_audit_row_written_with_the_right_action(db_session):
    plan = PeopleQuery(select=["id"], filters=[Filter(field="skills", op="contains", value="Terraform")])
    interp = Interpretation(entities=[_entity("skill", "Terraform")], unparsed=[])
    results, _ = search_people_ranked(db_session, HR, plan, interp)

    row = db_session.query(AuditLog).filter(AuditLog.action == "search_people_ranked").order_by(
        AuditLog.id.desc()).first()
    assert row is not None
    assert row.result_count == len(results)
    assert json.loads(row.fields_returned)  # non-empty SUMMARY_FIELDS list
