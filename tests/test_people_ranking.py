"""Tests for app/people_ranking.py -- SEARCH_RANKING_IMPLEMENTATION_PLAN.md
step 4 (step 6 folds in here too: SCORE_THRESHOLD/MAX_RANKED_RESULTS are
part of rank_candidates itself, not a follow-up edit -- see that plan's
design decision 4).

Same three-concern split as tests/test_team_builder.py:

  BOUNDARY    ranking never sees or scores an id outside what it was
              explicitly handed -- it operates on ids app.query_compiler
              already policy-filtered, and must not widen that set.
  ARITHMETIC  every weight, level, and band comparison the module claims
              to make, worked out by hand from the fixture below.
  DEGRADATION no candidates, a skill nobody holds, an absent term -- all
              three are normal outcomes, not errors.

Fixture data is created per test function via db_session, isolated by a
distinctive id/name prefix -- same pattern tests/test_team_builder.py uses,
so this file never depends on (or interferes with) conftest.py's session-
seeded data even though some fixture job titles/skill names below happen
to resemble it.
"""
from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from app.models import (
    Employee, EmployeeProject, EmployeeSkill, Office, OrgUnit, Project, ProjectSkillRequirement, Skill,
)
from app.models.enums import (
    AvailabilityStatus, EmploymentType, ProjectClassification, ProjectType, SkillCategory, SkillLevel, SkillSource,
)
from app.people_ranking import MAX_RANKED_RESULTS, load_pool, rank_candidates, score_candidate
from app.query_entities import Entity, Interpretation

PREFIX = "pr-fixture-"
NAME = "PR Fixture"


def _mkemp(db, key, org_unit_id, office_id, **overrides):
    fields = dict(
        id=f"{PREFIX}{key}", directory_object_id=None, full_name=f"{NAME} {key}",
        preferred_name=None, job_title="Consultant", org_unit_id=org_unit_id,
        office_id=office_id, manager_id=None, work_email=f"{PREFIX}{key}@example.test",
        work_phone=None, slack_handle=None, timezone=None,
        employment_type=EmploymentType.fte, hire_date=date(2020, 1, 1), cost_centre=None,
        personal_mobile=None, availability_status=AvailabilityStatus.available,
        away_until=None, delegate_id=None, bio=None, photo_url=None, is_active=True,
    )
    fields.update(overrides)
    emp = Employee(**fields)
    db.add(emp)
    return emp


def _entity(label: str, value: str) -> Entity:
    return Entity(label=label, span=(0, len(value)), text=value, value=value, confidence=1.0)


def _interp(*entities: Entity) -> Interpretation:
    return Interpretation(entities=list(entities), unparsed=[])


def _skill(db, emp, skill, level):
    db.add(EmployeeSkill(employee_id=emp.id, skill_id=skill.id, level=level,
                         source=SkillSource.confirmed, verified_at=None))


@pytest.fixture
def fx(db_session):
    """
        data_eng    "Data Engineer"          no skills, no seniority word
        senior      "Senior Data Engineer"   exact seniority band
        staff       "Staff Data Engineer"    adjacent seniority band
        junior      "Junior Data Engineer"   wrong (non-adjacent) band
        swe         "Software Engineer"      shares only the head noun
        analyst     "Financial Analyst"      no role/seniority overlap at all

        expert/working/learning/none_holder  "Consultant", ALPHA at
                                              Expert/Working/Learning/none

        both_working  ALPHA Working + BETA Working
        one_expert    ALPHA Expert only

        outsider      ALPHA Expert -- deliberately left OUT of every id set
                      a test hands to load_pool/rank_candidates

        recent/old    both ALPHA Expert; `recent` is on an ongoing project
                      declaring ALPHA, `old` is on the same project but it
                      ended ~30 months ago
    """
    db = db_session
    office = db.query(Office).first()
    unit = OrgUnit(name=f"{NAME} Unit", parent_id=None, unit_type="department")
    db.add(unit)
    db.flush()

    alpha = Skill(name=f"{NAME} Alpha", category=SkillCategory.technical, canonical_id=None)
    beta = Skill(name=f"{NAME} Beta", category=SkillCategory.technical, canonical_id=None)
    db.add_all([alpha, beta])
    db.flush()

    data_eng = _mkemp(db, "data-eng", unit.id, office.id, job_title="Data Engineer")
    senior = _mkemp(db, "senior", unit.id, office.id, job_title="Senior Data Engineer")
    staff = _mkemp(db, "staff", unit.id, office.id, job_title="Staff Data Engineer")
    junior = _mkemp(db, "junior", unit.id, office.id, job_title="Junior Data Engineer")
    swe = _mkemp(db, "swe", unit.id, office.id, job_title="Software Engineer")
    analyst = _mkemp(db, "analyst", unit.id, office.id, job_title="Financial Analyst")

    expert = _mkemp(db, "expert", unit.id, office.id)
    working = _mkemp(db, "working", unit.id, office.id)
    learning = _mkemp(db, "learning", unit.id, office.id)
    none_holder = _mkemp(db, "none", unit.id, office.id)

    both_working = _mkemp(db, "both-working", unit.id, office.id)
    one_expert = _mkemp(db, "one-expert", unit.id, office.id)

    outsider = _mkemp(db, "outsider", unit.id, office.id)

    recent = _mkemp(db, "recent", unit.id, office.id)
    old = _mkemp(db, "old", unit.id, office.id)
    db.flush()

    _skill(db, expert, alpha, SkillLevel.expert)
    _skill(db, working, alpha, SkillLevel.working)
    _skill(db, learning, alpha, SkillLevel.learning)

    _skill(db, both_working, alpha, SkillLevel.working)
    _skill(db, both_working, beta, SkillLevel.working)
    _skill(db, one_expert, alpha, SkillLevel.expert)

    _skill(db, outsider, alpha, SkillLevel.expert)

    _skill(db, recent, alpha, SkillLevel.expert)
    _skill(db, old, alpha, SkillLevel.expert)

    project = Project(name=f"{NAME} Project", type=ProjectType.project, description="",
                      owning_unit_id=unit.id, owner_id=recent.id, classification=ProjectClassification.internal)
    db.add(project)
    db.flush()
    db.add(ProjectSkillRequirement(project_id=project.id, skill_id=alpha.id, minimum_level=SkillLevel.working))
    db.flush()
    db.add(EmployeeProject(employee_id=recent.id, project_id=project.id, role="Contributor",
                           start_date=date(2024, 1, 1), end_date=None))
    db.add(EmployeeProject(employee_id=old.id, project_id=project.id, role="Contributor",
                           start_date=date(2015, 1, 1), end_date=date.today() - timedelta(days=900)))
    db.commit()

    emp_ids = [
        data_eng.id, senior.id, staff.id, junior.id, swe.id, analyst.id,
        expert.id, working.id, learning.id, none_holder.id,
        both_working.id, one_expert.id, outsider.id, recent.id, old.id,
    ]

    yield SimpleNamespace(
        alpha=alpha.name, beta=beta.name,
        data_eng=data_eng.id, senior=senior.id, staff=staff.id, junior=junior.id,
        swe=swe.id, analyst=analyst.id,
        expert=expert.id, working=working.id, learning=learning.id, none_holder=none_holder.id,
        both_working=both_working.id, one_expert=one_expert.id,
        outsider=outsider.id,
        recent=recent.id, old=old.id,
    )

    # Committed rows (unlike a plain flush) survive db_session's own
    # teardown (which just closes the session, no rollback) and would
    # otherwise persist for the rest of the test SESSION -- polluting
    # app.query_entities' live-database vocabulary scan for every other
    # test that parses free text (SEARCH_RANKING_IMPLEMENTATION_PLAN.md
    # step 4 depends on step 2's parser; a leftover "Senior Data Engineer"
    # job title here silently changed how a LATER test's "senior data
    # engineer" parsed). Same explicit-delete-by-prefix teardown
    # tests/test_team_builder.py's own `fx` fixture already uses.
    _cleanup(db, emp_ids=emp_ids, project_ids=[project.id])


def _cleanup(db, *, emp_ids: list[str], project_ids: list[int]) -> None:
    db.query(ProjectSkillRequirement).filter(ProjectSkillRequirement.project_id.in_(project_ids)).delete(
        synchronize_session=False)
    db.query(EmployeeProject).filter(EmployeeProject.project_id.in_(project_ids)).delete(synchronize_session=False)
    db.query(Project).filter(Project.id.in_(project_ids)).delete(synchronize_session=False)
    db.query(EmployeeSkill).filter(EmployeeSkill.employee_id.in_(emp_ids)).delete(synchronize_session=False)
    db.query(Skill).filter(Skill.name.like(f"{NAME}%")).delete(synchronize_session=False)
    db.query(Employee).filter(Employee.id.in_(emp_ids)).delete(synchronize_session=False)
    db.query(OrgUnit).filter(OrgUnit.name.like(f"{NAME}%")).delete(synchronize_session=False)
    db.commit()


# ---------------------------------------------------------------------------
# BOUNDARY
# ---------------------------------------------------------------------------

def test_ranking_never_sees_an_id_outside_what_it_was_given(db_session, fx):
    """`outsider` holds the same skill at the same level as `expert` but is
    deliberately excluded from the id set below -- proving the pool is
    built strictly from what it's given, not by re-querying for anyone who
    happens to match."""
    ids = frozenset({fx.expert, fx.working})
    pool = load_pool(db_session, ids)
    assert fx.outsider not in pool.employees

    ranked, _ = rank_candidates(pool, ids, _interp(_entity("skill", fx.alpha)))
    assert fx.outsider not in {c.employee_id for c in ranked}


def test_load_pool_of_no_ids_is_empty_not_an_error(db_session):
    pool = load_pool(db_session, frozenset())
    assert pool.employees == {}
    assert pool.skills_by_employee == {}
    assert pool.projects_by_employee == {}


# ---------------------------------------------------------------------------
# ARITHMETIC -- skills
# ---------------------------------------------------------------------------

def test_expert_outranks_working_outranks_learning_and_none_is_excluded(db_session, fx):
    ids = frozenset({fx.expert, fx.working, fx.learning, fx.none_holder})
    pool = load_pool(db_session, ids)
    ranked, _ = rank_candidates(pool, ids, _interp(_entity("skill", fx.alpha)))
    order = [c.employee_id for c in ranked]
    assert order.index(fx.expert) < order.index(fx.working) < order.index(fx.learning)
    assert fx.none_holder not in order  # 0% -- below SCORE_THRESHOLD, not an error


def test_two_skill_coverage_beats_single_skill_expertise_without_excluding_it(db_session, fx):
    """"holds both, one at Working" (70%) must outrank "holds only one, at
    Expert" (50%) -- coverage across requested skills dominates
    single-skill proficiency (design decision 1) -- while the one-skill
    holder still survives in the ranked result, not silently dropped."""
    ids = frozenset({fx.both_working, fx.one_expert})
    pool = load_pool(db_session, ids)
    interp = _interp(_entity("skill", fx.alpha), _entity("skill", fx.beta))
    ranked, any_holds_all = rank_candidates(pool, ids, interp)
    assert [c.employee_id for c in ranked] == [fx.both_working, fx.one_expert]
    assert any_holds_all is True  # fx.both_working holds both


# ---------------------------------------------------------------------------
# ARITHMETIC -- role
# ---------------------------------------------------------------------------

def test_role_score_tiers_exact_headnoun_and_no_match(db_session, fx):
    """The headline case: "Data Engineer" (exact) ranks above "Software
    Engineer" (shares only the head noun "Engineer"); "Financial Analyst"
    (no overlap at all) scores 0% and is excluded."""
    ids = frozenset({fx.data_eng, fx.swe, fx.analyst})
    pool = load_pool(db_session, ids)
    ranked, _ = rank_candidates(pool, ids, _interp(_entity("role", "Data Engineer")))
    order = [c.employee_id for c in ranked]
    assert order == [fx.data_eng, fx.swe]
    assert fx.analyst not in order


# ---------------------------------------------------------------------------
# ARITHMETIC -- seniority: exact / adjacent / wrong-band-is-a-real-penalty /
# absent-from-title-is-not-a-penalty
# ---------------------------------------------------------------------------

def test_seniority_tiers_and_absence_is_not_penalized(db_session, fx):
    """Asking for "senior": exact match (senior) and NO seniority word at
    all (data_eng) tie at 100% -- an unqualified title is "unspecified",
    not "definitely not senior", so it must not score lower than an exact
    match just for lacking the word. staff (adjacent band) scores between
    that and junior (a real, non-adjacent, wrong-band penalty)."""
    ids = frozenset({fx.senior, fx.staff, fx.junior, fx.data_eng})
    pool = load_pool(db_session, ids)
    interp = _interp(_entity("role", "Data Engineer"), _entity("seniority", "senior"))
    # Built directly via score_candidate, not rank_candidates, since this
    # test cares about the raw per-candidate numbers, not the cut/sort.
    scores = {eid: score_candidate(pool, pool.employees[eid], interp).score_pct for eid in ids}
    assert scores[fx.senior] == scores[fx.data_eng] == 100
    assert scores[fx.senior] > scores[fx.staff] > scores[fx.junior]


def test_recency_dropped_not_scored_zero_when_no_project_exists(db_session, fx):
    """A candidate with no declared-requirement project at all must not be
    diluted by a 0 on the recency term -- it has to be dropped and the
    remaining weight renormalised, same as an absent seniority term."""
    pool = load_pool(db_session, frozenset({fx.expert}))
    candidate = score_candidate(pool, pool.employees[fx.expert], _interp(_entity("skill", fx.alpha)))
    assert candidate.score_pct == 100  # skills term alone -- not diluted to 82%


def test_recent_project_outranks_an_old_one_for_the_same_skill(db_session, fx):
    ids = frozenset({fx.recent, fx.old})
    pool = load_pool(db_session, ids)
    ranked, _ = rank_candidates(pool, ids, _interp(_entity("skill", fx.alpha)))
    assert [c.employee_id for c in ranked] == [fx.recent, fx.old]


# ---------------------------------------------------------------------------
# DEGRADATION
# ---------------------------------------------------------------------------

def test_no_candidates_at_all_returns_empty_not_an_error(db_session):
    pool = load_pool(db_session, frozenset())
    ranked, any_holds_all = rank_candidates(pool, frozenset(), _interp(_entity("skill", "Nothing Seeded")))
    assert ranked == []
    assert any_holds_all is False


def test_a_skill_nobody_holds_scores_zero_not_an_error(db_session, fx):
    ids = frozenset({fx.expert})
    pool = load_pool(db_session, ids)
    ranked, any_holds_all = rank_candidates(pool, ids, _interp(_entity("skill", "Nothing This Pool Holds")))
    assert ranked == []  # 0% -- below threshold, not an exception
    assert any_holds_all is False


def test_zero_overlap_flag_fires_only_when_actually_true(db_session, fx):
    ids_both = frozenset({fx.both_working, fx.one_expert})
    pool_both = load_pool(db_session, ids_both)
    _, someone_holds_both = rank_candidates(
        pool_both, ids_both, _interp(_entity("skill", fx.alpha), _entity("skill", fx.beta)))
    assert someone_holds_both is True  # fx.both_working holds both

    ids_neither = frozenset({fx.expert, fx.learning})  # neither holds BETA at all
    pool_neither = load_pool(db_session, ids_neither)
    _, nobody_holds_both = rank_candidates(
        pool_neither, ids_neither, _interp(_entity("skill", fx.alpha), _entity("skill", fx.beta)))
    assert nobody_holds_both is False


# ---------------------------------------------------------------------------
# CAP -- step 6, folded into rank_candidates itself
# ---------------------------------------------------------------------------

def test_more_than_twenty_above_threshold_truncates_at_twenty(db_session):
    db = db_session
    office = db.query(Office).first()
    unit = db.query(OrgUnit).first()
    skill = Skill(name=f"{NAME} Cap Skill", category=SkillCategory.technical, canonical_id=None)
    db.add(skill)
    db.flush()

    ids = []
    for i in range(25):
        emp = _mkemp(db, f"cap-{i}", unit.id, office.id)
        db.flush()
        _skill(db, emp, skill, SkillLevel.expert)
        ids.append(emp.id)
    db.commit()

    ids = frozenset(ids)
    pool = load_pool(db, ids)
    ranked, _ = rank_candidates(pool, ids, _interp(_entity("skill", skill.name)))
    assert len(ranked) == MAX_RANKED_RESULTS
    _cleanup(db, emp_ids=list(ids), project_ids=[])


def test_fewer_than_the_old_flat_cap_of_five_are_all_returned(db_session):
    """RC3 fix: MAX_SEARCH_RESULTS=5 (the old flat cutoff for Azure
    Search's RRF path) does not apply here -- 8 candidates all above
    threshold must all come back, not just the first 5."""
    db = db_session
    office = db.query(Office).first()
    unit = db.query(OrgUnit).first()
    skill = Skill(name=f"{NAME} RC3 Skill", category=SkillCategory.technical, canonical_id=None)
    db.add(skill)
    db.flush()

    ids = []
    for i in range(8):
        emp = _mkemp(db, f"rc3-{i}", unit.id, office.id)
        db.flush()
        _skill(db, emp, skill, SkillLevel.expert)
        ids.append(emp.id)
    db.commit()

    ids = frozenset(ids)
    pool = load_pool(db, ids)
    ranked, _ = rank_candidates(pool, ids, _interp(_entity("skill", skill.name)))
    assert len(ranked) == 8
    _cleanup(db, emp_ids=list(ids), project_ids=[])
