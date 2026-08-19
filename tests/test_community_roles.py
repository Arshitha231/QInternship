"""The seven canonical community-graph roles (app/community_roles.py).

Builds its own miniature org — real cities, so the distance table applies —
rather than leaning on conftest's fixture set, which has one office and
therefore can't express "your office has nobody, who's nearest?". Everything
it creates is torn down again: the fixture database is session-scoped, so a
leftover employee would follow every test that runs after these.
"""
from datetime import date

import pytest

from app.auth import AuthenticatedUser
from app.community_links import list_community_links
from app.models import (
    CommunityLink, Employee, EmployeeProject, EmployeeSkill, Office, OrgUnit, Project, Skill,
)
from app.models.enums import (
    AvailabilityStatus, CommunityLinkSource, EmploymentType, ProjectClassification,
    ProjectType, SkillCategory, SkillLevel, SkillSource,
)

# Real cities, because app.community_roles.CITY_COORDINATES is keyed by
# city name and the point of these tests is which one is nearest.
# Bangalore->Singapore is ~3,100km; Bangalore->London ~8,000km.
CITIES = ["Bangalore", "Singapore", "London", "Seattle"]


@pytest.fixture
def org(db_session, monkeypatch):
    """A throwaway org: four offices, an HR unit, an IT unit, and an
    engineering unit to hang ordinary people off."""
    monkeypatch.setenv("HR_ORG_UNIT_NAME", "Roles Test HR")
    monkeypatch.setenv("IT_ORG_UNIT_NAME", "Roles Test IT")

    created = []

    def add(obj):
        db_session.add(obj)
        db_session.flush()
        created.append(obj)
        return obj

    offices = {
        city: add(Office(name=f"{city} Roles Office", city=city, country="X", timezone="UTC"))
        for city in CITIES
    }
    company = add(OrgUnit(name="Roles Test Co", parent_id=None, unit_type="company"))
    units = {
        key: add(OrgUnit(name=name, parent_id=company.id, unit_type="department"))
        for key, name in (("hr", "Roles Test HR"), ("it", "Roles Test IT"), ("eng", "Roles Test Eng"))
    }
    # A team under HR, to prove membership is inherited down the subtree.
    units["hr_team"] = add(OrgUnit(name="Roles Test HR Team", parent_id=units["hr"].id, unit_type="team"))

    counter = {"n": 0}

    def person(city, unit_key, *, title="Software Engineer", manager=None, hired=date(2020, 1, 1)):
        counter["n"] += 1
        return add(Employee(
            id=f"roles-{counter['n']}", full_name=f"Person {counter['n']}", job_title=title,
            org_unit_id=units[unit_key].id, office_id=offices[city].id,
            manager_id=manager.id if manager else None,
            work_email=f"roles-{counter['n']}@example.test", employment_type=EmploymentType.fte,
            hire_date=hired, availability_status=AvailabilityStatus.available, is_active=True,
        ))

    db_session.commit()
    yield {"offices": offices, "units": units, "person": person, "add": add, "db": db_session}

    for obj in reversed(created):
        db_session.delete(obj)
    db_session.commit()


def graph(db, owner) -> dict[str, dict]:
    """The owner's resolved roles, keyed by role_key."""
    user = AuthenticatedUser(id=owner.id, role="employee", name=owner.full_name)
    return {
        row["role_key"]: row
        for row in list_community_links(db, user, "employee")
        if row["role_key"] is not None
    }


# --- geography ------------------------------------------------------------

def test_own_office_wins_over_any_other(org):
    db = org["db"]
    owner = org["person"]("Bangalore", "eng")
    local = org["person"]("Bangalore", "hr", title="HR Generalist")
    org["person"]("Singapore", "hr", title="HR Generalist")
    db.commit()

    hr = graph(db, owner)["hr_rep"]
    assert hr["contact_employee_id"] == local.id
    assert hr["is_remote_fallback"] is False
    assert hr["distance_km"] is None


def test_falls_back_to_the_nearest_office_not_just_any(org):
    """The whole point of measuring distance: Bangalore's nearest HR is
    Singapore (~3,100km), not London (~8,000km). Without coordinates this
    would come down to whichever row sorted first."""
    db = org["db"]
    owner = org["person"]("Bangalore", "eng")
    org["person"]("London", "hr", title="HR Generalist")
    near = org["person"]("Singapore", "hr", title="HR Generalist")
    db.commit()

    hr = graph(db, owner)["hr_rep"]
    assert hr["contact_employee_id"] == near.id
    assert hr["is_remote_fallback"] is True
    assert hr["contact_office_city"] == "Singapore"
    assert 2500 < hr["distance_km"] < 3800


def test_a_role_nobody_anywhere_fills_is_absent_not_empty(org):
    db = org["db"]
    owner = org["person"]("Bangalore", "eng")
    db.commit()
    assert "it_rep" not in graph(db, owner)


def test_distance_is_not_a_fallback_for_a_role_that_does_not_widen_by_it(org):
    """A same-team expert in another city is the best answer, not a
    consolation prize — reporting them as a fallback would say the directory
    came up short when it didn't."""
    db = org["db"]
    skill = org["add"](Skill(name="Roles Test Skill", category=SkillCategory.technical, canonical_id=None))
    owner = org["person"]("Bangalore", "eng")
    expert = org["person"]("Seattle", "eng")
    org["add"](EmployeeSkill(employee_id=owner.id, skill_id=skill.id, level=SkillLevel.learning,
                             source=SkillSource.self_reported))
    org["add"](EmployeeSkill(employee_id=expert.id, skill_id=skill.id, level=SkillLevel.expert,
                             source=SkillSource.confirmed))
    db.commit()

    tech = graph(db, owner)["technical_expert"]
    assert tech["contact_employee_id"] == expert.id
    assert tech["is_remote_fallback"] is False
    assert tech["contact_office_city"] == "Seattle"


# --- who gets picked ------------------------------------------------------

def test_hr_confirmed_link_beats_a_derived_contact(org):
    """Confirming a suggestion is HR's decision and outranks the guess."""
    db = org["db"]
    owner = org["person"]("Bangalore", "eng")
    org["person"]("Bangalore", "hr", title="HR Generalist")
    chosen = org["person"]("London", "eng", title="Software Engineer")
    org["add"](CommunityLink(
        owner_employee_id=owner.id, contact_employee_id=chosen.id, role_label="hr_contact",
        reason=None, source=CommunityLinkSource.official, is_mentor_link=False,
    ))
    db.commit()

    assert graph(db, owner)["hr_rep"]["contact_employee_id"] == chosen.id


def test_hr_unit_membership_is_inherited_down_the_subtree(org):
    db = org["db"]
    owner = org["person"]("Bangalore", "eng")
    on_team = org["person"]("Bangalore", "hr_team", title="HR Coordinator")
    db.commit()
    assert graph(db, owner)["hr_rep"]["contact_employee_id"] == on_team.id


def test_one_person_does_not_fill_both_security_and_it(org):
    """An Identity & Access Analyst matches the security keywords AND sits in
    the IT division. Two nodes for one human looks like a bug and hides the
    fact that there's a separate contact to know about."""
    db = org["db"]
    owner = org["person"]("Bangalore", "eng")
    both = org["person"]("Bangalore", "it", title="Identity & Access Analyst")
    other_it = org["person"]("Bangalore", "it", title="Service Desk Engineer")
    db.commit()

    resolved = graph(db, owner)
    assert resolved["security_rep"]["contact_employee_id"] == both.id
    assert resolved["it_rep"]["contact_employee_id"] == other_it.id


def test_one_person_fills_both_when_they_are_genuinely_the_only_one(org):
    """Naming them twice beats leaving a role unanswered in a small office."""
    db = org["db"]
    owner = org["person"]("Bangalore", "eng")
    only = org["person"]("Bangalore", "it", title="Identity & Access Analyst")
    db.commit()

    resolved = graph(db, owner)
    assert resolved["security_rep"]["contact_employee_id"] == only.id
    assert resolved["it_rep"]["contact_employee_id"] == only.id


def test_technical_expert_is_never_the_manager_or_mentor(org):
    db = org["db"]
    skill = org["add"](Skill(name="Roles Test Skill 2", category=SkillCategory.technical, canonical_id=None))
    boss = org["person"]("Bangalore", "eng")
    owner = org["person"]("Bangalore", "eng", manager=boss)
    org["add"](EmployeeSkill(employee_id=owner.id, skill_id=skill.id, level=SkillLevel.working,
                             source=SkillSource.self_reported))
    # The manager is an expert in it — and is already on the graph.
    org["add"](EmployeeSkill(employee_id=boss.id, skill_id=skill.id, level=SkillLevel.expert,
                             source=SkillSource.confirmed))
    db.commit()

    resolved = graph(db, owner)
    assert resolved["manager"]["contact_employee_id"] == boss.id
    assert "technical_expert" not in resolved


def test_project_contact_is_the_owner_of_a_project_you_are_on(org):
    db = org["db"]
    lead = org["person"]("Seattle", "eng", title="Director")
    owner = org["person"]("Bangalore", "eng")
    project = org["add"](Project(
        name="Roles Test Project", type=ProjectType.project, owning_unit_id=org["units"]["eng"].id,
        owner_id=lead.id, classification=ProjectClassification.internal, is_client_engagement=False,
    ))
    org["add"](EmployeeProject(employee_id=owner.id, project_id=project.id, role="Engineer",
                               start_date=date(2023, 2, 1), end_date=None))
    db.commit()

    assert graph(db, owner)["project_contact"]["contact_employee_id"] == lead.id


def test_you_are_never_your_own_project_contact(org):
    db = org["db"]
    owner = org["person"]("Bangalore", "eng")
    project = org["add"](Project(
        name="Roles Test Own Project", type=ProjectType.project, owning_unit_id=org["units"]["eng"].id,
        owner_id=owner.id, classification=ProjectClassification.internal, is_client_engagement=False,
    ))
    org["add"](EmployeeProject(employee_id=owner.id, project_id=project.id, role="Owner",
                               start_date=date(2023, 2, 1), end_date=None))
    db.commit()

    assert "project_contact" not in graph(db, owner)


# --- what the graph returns ----------------------------------------------

def test_the_four_hr_official_rows_collapse_into_one_node(org):
    """payroll, benefits, facilities and general HR were four nodes
    answering one question. The rows still exist and still win; they are
    presented as the single role they always were."""
    db = org["db"]
    owner = org["person"]("Bangalore", "eng")
    hr_person = org["person"]("Bangalore", "hr", title="HR Generalist")
    payroll_person = org["person"]("Bangalore", "eng", title="Payroll Analyst")
    for label, contact in (("hr_contact", hr_person), ("payroll", payroll_person)):
        org["add"](CommunityLink(
            owner_employee_id=owner.id, contact_employee_id=contact.id, role_label=label,
            reason=None, source=CommunityLinkSource.official, is_mentor_link=False,
        ))
    db.commit()

    user = AuthenticatedUser(id=owner.id, role="employee", name=owner.full_name)
    rows = list_community_links(db, user, "employee")
    assert [r["role_label"] for r in rows].count("hr_rep") == 1
    assert "payroll" not in [r["role_label"] for r in rows]
    # HR's own preference order: hr_contact outranks payroll for the one slot.
    assert graph(db, owner)["hr_rep"]["contact_employee_id"] == hr_person.id


def test_personal_links_are_untouched_by_any_of_this(org):
    db = org["db"]
    owner = org["person"]("Bangalore", "eng")
    friend = org["person"]("Bangalore", "eng")
    org["add"](CommunityLink(
        owner_employee_id=owner.id, contact_employee_id=friend.id, role_label="helps with CI",
        reason="knows the pipeline", source=CommunityLinkSource.personal, is_mentor_link=False,
    ))
    db.commit()

    user = AuthenticatedUser(id=owner.id, role="employee", name=owner.full_name)
    personal = [r for r in list_community_links(db, user, "employee") if r["source"] == "personal"]
    assert len(personal) == 1
    assert personal[0]["role_label"] == "helps with CI"
    assert personal[0]["role_key"] is None
