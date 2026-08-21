"""app/text_filters.py -- reading plain free text as a structured
PeopleQuery, using only vocabulary that exists in the database.

Exercised against the real conftest.py fixture data, same as
tests/test_query_compiler.py: offices "Test HQ"/Testville and
"Satellite Office"/"Satellite City"; org units "Engineering",
"Platform Engineering", "Finance Operations"; skills "Terraform",
"Power BI", "French", "Site Reliability Engineering"; job titles
"Software Engineer", "Engineering Manager", "Data Analyst",
"Infrastructure Engineer", "Financial Analyst", "Legal Counsel".

The behaviour these pin down is deliberately conservative: this module
only ever runs after the direct path has already returned nothing, so its
job is to find a real request in the text or get out of the way. Every
test below is either "it found the real values" or "it correctly found
nothing".
"""
from app.people import MAX_RESULTS, SUMMARY_FIELDS
from app.text_filters import plan_from_text

SELECT = sorted(SUMMARY_FIELDS)


def _plan(db_session, text):
    return plan_from_text(db_session, text, select_fields=SELECT, limit=MAX_RESULTS)


def _filters(plan) -> set[tuple[str, str, str | frozenset[str]]]:
    # frozenset, not tuple: an "in" filter's value list is unordered from
    # the caller's perspective (apply_filter ORs every value regardless of
    # order), and _match_all's own order is an implementation detail
    # (longest value first) that these tests shouldn't pin down.
    return {(f.field, f.op, frozenset(f.value) if isinstance(f.value, list) else f.value) for f in plan.filters}


# ---------------------------------------------------------------------------
# The case this module was written for
# ---------------------------------------------------------------------------

def test_title_and_office_are_both_read_out_of_one_phrase(db_session):
    """"engineers in Testville" is a two-filter structured request wearing
    plain English. Neither half is a name, which is all find_people's SQL
    fallback can match -- hence the zero results this exists to fix."""
    plan = _plan(db_session, "engineers in Testville")
    assert plan is not None
    # "Engineer", real-cased -- app/query_entities.py's role candidates are
    # job-title n-grams spelled the way a real job_title spells them, not
    # a lowercased single word. Functionally identical under ilike; a
    # different literal string than this test asserted before that module
    # existed.
    assert _filters(plan) == {
        ("office", "contains", "Testville"),
        ("job_title", "contains", "Engineer"),
    }


def test_plural_resolves_to_the_singular_the_titles_actually_use(db_session):
    """Nobody's job_title is "Engineers". The plural has to reach the
    singular or the most natural phrasing matches nothing."""
    plan = _plan(db_session, "analysts in Satellite City")
    assert plan is not None
    assert ("job_title", "contains", "Analyst") in _filters(plan)  # real-cased, see above
    assert ("office", "contains", "Satellite City") in _filters(plan)


def test_a_bare_skill_name_is_recognised(db_session):
    """A one-word query that happens to be a real skill is a filter
    request, not a name lookup."""
    plan = _plan(db_session, "Terraform")
    assert plan is not None
    assert _filters(plan) == {("skills", "contains", "Terraform")}


def test_two_skills_become_one_in_filter(db_session):
    """"Terraform, Kubernetes" names two real skills that nobody in the
    fixture holds together. This must still produce one OR-of-both filter
    (apply_filter's skills branch ORs every value in an "in" list
    regardless of op) rather than only the first or the longest match --
    that's the fix for multi-skill queries like "react, java"."""
    plan = _plan(db_session, "Terraform, Kubernetes")
    assert plan is not None
    assert _filters(plan) == {("skills", "in", frozenset({"Terraform", "Kubernetes"}))}


# ---------------------------------------------------------------------------
# Getting out of the way -- the half that keeps this safe
# ---------------------------------------------------------------------------

def test_text_naming_nothing_real_returns_none(db_session):
    """None, not an empty plan. An empty plan would compile to "everyone",
    turning a failed search into a full directory listing."""
    assert _plan(db_session, "Nobody Named This In The Whole Company") is None


def test_a_vague_description_matches_nothing(db_session):
    """The query three tests in test_unified_search.py use to assert the
    model is never called for ordinary free text. It must not accidentally
    become a filter request here either -- "dashboards" and "reporting" are
    not job titles, org units, offices or skills in this database."""
    assert _plan(db_session, "someone good with dashboards and reporting") is None


def test_noise_words_alone_do_not_make_a_title_filter(db_session):
    """"team" occurs in real job titles but would match most of them, so
    it is stoplisted. On its own it must leave nothing behind."""
    assert _plan(db_session, "the team") is None


def test_matching_is_whole_word_not_substring(db_session):
    """Bare substring matching would let the skill "French" fire on
    "Frenchman" and the office "Testville" on "Testvilles" -- a confident
    filter built on a word the user never used."""
    assert _plan(db_session, "Frenchman") is None


# ---------------------------------------------------------------------------
# Specificity
# ---------------------------------------------------------------------------

def test_the_most_specific_org_unit_wins(db_session):
    """"Platform Engineering" and "Engineering" are both real units and one
    contains the other. The longer one is the one actually named."""
    plan = _plan(db_session, "people in Platform Engineering")
    assert plan is not None
    assert ("org_unit", "eq", "Platform Engineering") in _filters(plan)
    assert ("org_unit", "eq", "Engineering") not in _filters(plan)


def test_the_plan_carries_the_caller_agnostic_shape(db_session):
    """A plan is inert until enforce() rules on it (app/query_plan.py). It
    must therefore select only the summary fields and carry the same cap
    the direct path uses -- never decide visibility itself."""
    plan = _plan(db_session, "engineers in Testville")
    assert plan is not None
    assert plan.select == SELECT
    assert plan.limit == MAX_RESULTS
