"""app/query_entities.py -- typing free text into role/seniority/skill/
office/org_unit entities over one non-overlapping-span scan.

Exercised against the same conftest.py fixture as test_text_filters.py,
plus its job titles that matter here specifically: "Data Engineer"
(extract-dup-2), "Staff Engineer" (conf-owner-1), and "VP of Engineering"
(chain-3/rchain-3) -- each one a real multi-word title that shares its
first word with a real seniority band word ("staff", "vp") or would
otherwise be read as a single generic title word ("engineer").
"""
from app.query_entities import parse

SENIORITY = "seniority"
ROLE = "role"


def _labelled(interpretation, label):
    return {e.value for e in interpretation.entities if e.label == label}


# ---------------------------------------------------------------------------
# The headline case
# ---------------------------------------------------------------------------

def test_role_and_seniority_are_separate_entities(db_session):
    """"senior data engineer" must type as a role entity ("Data Engineer")
    and a separate seniority entity ("senior") -- not one flattened
    job_title-contains-"engineer" guess, which is the bug
    SEARCH_RANKING_PROPOSAL.md diagnoses."""
    interpretation = parse(db_session, "senior data engineer")
    assert _labelled(interpretation, ROLE) == {"Data Engineer"}
    assert _labelled(interpretation, SENIORITY) == {"senior"}
    assert interpretation.unparsed == []


# ---------------------------------------------------------------------------
# Non-overlapping spans: the real multi-word title wins its words first
# ---------------------------------------------------------------------------

def test_two_word_title_claims_its_seniority_look_alike_word(db_session):
    """"Staff Engineer" is a real job title AND "staff" is a real
    seniority band word. The longer, more specific candidate has to claim
    both words before "staff" gets a separate turn at the first one."""
    interpretation = parse(db_session, "staff engineer")
    assert _labelled(interpretation, ROLE) == {"Staff Engineer"}
    assert _labelled(interpretation, SENIORITY) == set()


def test_multiword_title_claims_its_seniority_look_alike_word(db_session):
    """Same shape, three words: "VP of Engineering" is a real job title
    AND "VP" is a real seniority band word."""
    interpretation = parse(db_session, "who is the VP of Engineering")
    assert _labelled(interpretation, ROLE) == {"VP of Engineering"}
    assert _labelled(interpretation, SENIORITY) == set()


# ---------------------------------------------------------------------------
# Unparsed reporting
# ---------------------------------------------------------------------------

def test_unresolved_words_are_reported_not_silently_dropped(db_session):
    """A word with no match in any label's vocabulary still gets surfaced,
    so the chip row (step 3) can show the user what it couldn't place."""
    interpretation = parse(db_session, "senior data engineer, unicorns")
    assert _labelled(interpretation, ROLE) == {"Data Engineer"}
    assert _labelled(interpretation, SENIORITY) == {"senior"}
    assert interpretation.unparsed == ["unicorns"]


# ---------------------------------------------------------------------------
# No real vocabulary at all
# ---------------------------------------------------------------------------

def test_no_real_vocabulary_returns_an_empty_interpretation(db_session):
    """Every word here is either too short or a stoplisted connector --
    there is no real request hiding in this text at all."""
    interpretation = parse(db_session, "the and for")
    assert interpretation.entities == []
    assert interpretation.unparsed == []
