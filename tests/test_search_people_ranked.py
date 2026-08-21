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


def test_match_explanation_matches_the_ranked_candidate_that_produced_it(db_session):
    """SEARCH_RANKING_IMPLEMENTATION_PLAN.md step 5: `match` on a ranked
    result must be the same score/matched/missing that decided its
    position, never a separately recomputed value."""
    plan = PeopleQuery(select=["id"], filters=[Filter(field="skills", op="contains", value="Terraform")])
    interp = Interpretation(entities=[_entity("skill", "Terraform")], unparsed=[])
    results, _ = search_people_ranked(db_session, HR, plan, interp)

    by_id = {p.id: p for p in results}
    assert by_id["search-filter-eng"].match.score_pct == 100
    assert by_id["search-filter-eng"].match.matched == ["Terraform (Expert)"]
    assert by_id["search-filter-eng"].match.missing == []
    # Learning still survives (above SCORE_THRESHOLD) but scores lower --
    # the card explains why, not just that it ranks second.
    assert 0 < by_id["search-filter-fin"].match.score_pct < 100


def test_match_is_absent_not_null_on_the_unranked_path(db_session):
    """search_people_by_plan never sets `match` at all -- PersonSummary's
    own field default (None, unset) means the JSON body genuinely omits
    the key rather than emitting `"match": null`, the same absent-not-null
    convention every other conditional field on this model follows. There
    is no caller who can see people but not `match` specifically to test
    a permission boundary against (skills is BASE_FIELDS, visible to
    every role) -- this documents the field's own always-real-or-absent
    construction instead."""
    from app.people import search_people_by_plan

    plan = PeopleQuery(select=["id"], filters=[Filter(field="skills", op="contains", value="Terraform")])
    results = search_people_by_plan(db_session, HR, plan)
    assert results
    assert all(p.match is None for p in results)
    assert all("match" not in p.model_dump(exclude_unset=True) for p in results)


def test_audit_row_written_with_the_right_action(db_session):
    plan = PeopleQuery(select=["id"], filters=[Filter(field="skills", op="contains", value="Terraform")])
    interp = Interpretation(entities=[_entity("skill", "Terraform")], unparsed=[])
    results, _ = search_people_ranked(db_session, HR, plan, interp)

    row = db_session.query(AuditLog).filter(AuditLog.action == "search_people_ranked").order_by(
        AuditLog.id.desc()).first()
    assert row is not None
    assert row.result_count == len(results)
    assert json.loads(row.fields_returned)  # non-empty SUMMARY_FIELDS list
