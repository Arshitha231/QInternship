"""resolve_skill's alias fallback (app/people.py's SKILL_ALIASES).

"finance"/"accounting" name real skills (Financial Modeling, GAAP
Accounting) but share no usable prefix or substring with them --
"financ-e" vs "financ-ial" diverge at the 7th character -- so before this
fallback existed, find_mentor/skill_gap/skill_scarcity confidently reported
zero matches for a request the directory could actually answer. See
app.people.SKILL_ALIASES for why this is a small hand-curated table rather
than a fuzzy-matching tier: the same substring-inflation false positive
just fixed in app.directory_tools.resolve_project_name (a long, unrelated
string scoring deceptively high against a short name purely because of a
shared word) rules out reusing WRatio/partial_ratio here too.

Fixture data uses its own prefixed Skill/Employee rows, same pattern as
tests/test_assistant_context.py -- "Financial Modeling"/"GAAP Accounting"
are part of seed.py's large synthetic dataset, not conftest.py's small
hermetic test fixture set, so this test module has to seed them itself.
"""
from __future__ import annotations

from datetime import date, datetime

import pytest

from app.auth import AuthenticatedUser
from app.directory_tools import find_mentor
from app.models import Employee, EmployeeSkill, Office, OrgUnit, Skill
from app.models.enums import AvailabilityStatus, EmploymentType, SkillCategory, SkillLevel, SkillSource
from app.people import resolve_skill

PREFIX = "skillres-fixture-"

HR = AuthenticatedUser(id=f"{PREFIX}hr-caller", role="hr", name="Test HR")


@pytest.fixture
def finance_skills(db_session):
    db = db_session
    org_unit = db.query(OrgUnit).filter_by(name="Platform Engineering").first()
    office = db.query(Office).first()

    modeling = Skill(name="Financial Modeling", category=SkillCategory.technical, canonical_id=None)
    accounting = Skill(name="GAAP Accounting", category=SkillCategory.technical, canonical_id=None)
    db.add_all([modeling, accounting])
    db.flush()

    mentor = Employee(
        id=f"{PREFIX}mentor", directory_object_id=None, full_name="Fixture Mentor", preferred_name=None,
        job_title="Finance Lead", org_unit_id=org_unit.id, office_id=office.id, manager_id=None,
        work_email=f"{PREFIX}mentor@example.test", work_phone=None, slack_handle=None, timezone=None,
        employment_type=EmploymentType.fte, hire_date=date(2022, 1, 1), cost_centre=None,
        personal_mobile=None, availability_status=AvailabilityStatus.available,
        away_until=None, delegate_id=None, bio=None, photo_url=None, is_active=True,
    )
    db.add(mentor)
    db.flush()
    db.add_all([
        EmployeeSkill(employee_id=mentor.id, skill_id=modeling.id, level=SkillLevel.expert,
                      source=SkillSource.confirmed, verified_at=datetime.now()),
        EmployeeSkill(employee_id=mentor.id, skill_id=accounting.id, level=SkillLevel.expert,
                      source=SkillSource.confirmed, verified_at=datetime.now()),
    ])
    db.commit()

    yield modeling, accounting

    db.query(EmployeeSkill).filter(EmployeeSkill.employee_id == mentor.id).delete()
    db.query(Employee).filter(Employee.id == mentor.id).delete()
    db.query(Skill).filter(Skill.id.in_([modeling.id, accounting.id])).delete()
    db.commit()


@pytest.mark.parametrize("query,canonical", [
    ("finance", "Financial Modeling"),
    ("Finance", "Financial Modeling"),
    ("FINANCE", "Financial Modeling"),
    ("financials", "Financial Modeling"),
    ("accounting", "GAAP Accounting"),
    ("Accounting", "GAAP Accounting"),
    ("accountant", "GAAP Accounting"),
    ("accountancy", "GAAP Accounting"),
])
def test_alias_resolves_to_the_real_skill(db_session, finance_skills, query, canonical):
    resolved = resolve_skill(db_session, query)
    assert resolved is not None
    assert resolved.name == canonical


def test_exact_and_synonym_matches_are_unaffected(db_session, finance_skills):
    # A real skill name, and a real seeded SKILL_CANONICAL_MAP synonym --
    # the alias fallback must never be consulted when the direct lookup
    # already succeeds, and must not shadow a real skill named "Financial
    # Modeling" itself.
    assert resolve_skill(db_session, "Financial Modeling").name == "Financial Modeling"
    assert resolve_skill(db_session, "Site Reliability Engineering").name == "Site Reliability Engineering"


def test_unrelated_word_still_resolves_to_nothing(db_session, finance_skills):
    assert resolve_skill(db_session, "zzzz quantum flibbertigibbet") is None


def test_find_mentor_answers_for_the_aliased_skill(db_session, finance_skills):
    # The reported symptom end to end: "find a mentor for finance" (and
    # accounting) coming back with zero candidates even though someone with
    # Financial Modeling / GAAP Accounting exists in the directory.
    caller_id = f"{PREFIX}some-other-caller"
    for skill in ("finance", "accounting"):
        candidates = find_mentor(db_session, HR, skill=skill, caller_id=caller_id)
        assert candidates, f"expected at least one mentor candidate for {skill!r}"
        assert candidates[0].full_name == "Fixture Mentor"
