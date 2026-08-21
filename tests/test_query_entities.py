"""app/query_entities.py -- typing free text into role/seniority/skill/
office/org_unit entities over one non-overlapping-span scan.

Exercised against the same conftest.py fixture as test_text_filters.py,
plus its job titles that matter here specifically: "Data Engineer"
(extract-dup-2), "Staff Engineer" (conf-owner-1), and "VP of Engineering"
(chain-3/rchain-3) -- each one a real multi-word title that shares its
first word with a real seniority band word ("staff", "vp") or would
otherwise be read as a single generic title word ("engineer").
"""
from datetime import date

import pytest

from app.models import Employee, Office, OrgUnit
from app.models.enums import AvailabilityStatus, EmploymentType
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
# A length tie between a bare role word and the seniority word it's spelled
# identically to -- confidence, not insertion order, must break it
# ---------------------------------------------------------------------------

@pytest.fixture
def senior_consultant(db_session):
    """A second real title starting with the bare word "Senior" --
    deliberately NOT "Senior Data Engineer", which would let the whole
    three-word phrase win outright as one longer, more specific title
    match and mask the actual bug. "Senior Consultant" only contributes a
    bare "Senior" role candidate (confidence 0.5, from the 1-word n-gram)
    that is exactly as long as the "senior" seniority band word
    (confidence 1.0) once "Data Engineer" has already claimed its own two
    words -- the tie query_entities.parse must resolve toward the
    higher-confidence reading, not toward whichever list happened to be
    built first.
    """
    db = db_session
    unit = db.query(OrgUnit).first()
    office = db.query(Office).first()
    emp = Employee(
        id="qe-fixture-senior-consultant", directory_object_id=None,
        full_name="QE Fixture Senior Consultant", preferred_name=None,
        job_title="Senior Consultant", org_unit_id=unit.id, office_id=office.id,
        manager_id=None, work_email="qe-fixture-senior-consultant@example.test",
        work_phone=None, slack_handle=None, timezone=None,
        employment_type=EmploymentType.fte, hire_date=date(2020, 1, 1), cost_centre=None,
        personal_mobile=None, availability_status=AvailabilityStatus.available,
        away_until=None, delegate_id=None, bio=None, photo_url=None, is_active=True,
    )
    db.add(emp)
    db.commit()
    yield emp
    db.query(Employee).filter(Employee.id == emp.id).delete(synchronize_session=False)
    db.commit()


def test_a_bare_role_word_does_not_win_a_tie_against_the_identical_seniority_word(
    db_session, senior_consultant,
):
    """On a directory where some real title starts with "Senior", the bare
    "Senior" role candidate this produces must not preempt the seniority
    band word of the same length -- "senior data engineer" still has to
    split into role="Data Engineer" + seniority="senior", not collapse
    into two role entities ("Senior" and "Data Engineer") and zero
    seniority entities, which is what a plain length-only sort produced."""
    interpretation = parse(db_session, "senior data engineer")
    assert _labelled(interpretation, ROLE) == {"Data Engineer"}
    assert _labelled(interpretation, SENIORITY) == {"senior"}


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
