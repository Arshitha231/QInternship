"""Everyone edits their OWN skills and languages — and nobody else's.

Three rules, tested independently because they fail independently:

  1. Identity is the gate, not role. An ordinary employee can add, re-level
     and remove entries on their own profile; hr and it get no extra reach
     over anybody else's, which is the half a role-keyed gate would have
     got backwards.
  2. Every write is `self`-sourced. A re-level RE-STAMPS an entry that
     arrived `confirmed`/`certified`, clearing verified_at with it — the
     directory must never show a self-claimed level wearing somebody else's
     attestation.
  3. Skills and languages are one table split by category, so the same
     endpoints serve both, and a name filed under the wrong category is
     refused rather than silently landing in the other card.

Every denial below calls the endpoint directly with whatever role and id it
likes: there is no preceding read to have been filtered and no frontend to
have hidden a button, same standard as tests/test_write_endpoints.py.
"""
from datetime import date, datetime

import pytest

from app.models import AuditLog, Employee, EmployeeSkill, Office, OrgUnit, Skill
from app.models.enums import (
    AvailabilityStatus, EmploymentType, SkillCategory, SkillLevel, SkillSource,
)
from tests.conftest import auth_headers

ALL_ROLES = ["employee", "manager", "hr", "it"]


def _mkemp(db_session, id_, full_name, **overrides) -> Employee:
    """Throwaway employee per test — never a shared conftest fixture
    person, since the test database is session-scoped and every test here
    mutates its subject's skill rows."""
    existing = db_session.get(Employee, id_)
    if existing is not None:
        return existing
    office = db_session.query(Office).filter(Office.name == "Test HQ").one()
    org_unit = db_session.query(OrgUnit).filter(OrgUnit.name == "Platform Engineering").one()
    emp = Employee(
        id=id_, directory_object_id=None, full_name=full_name, preferred_name=None,
        job_title="Software Engineer", org_unit_id=org_unit.id, office_id=office.id,
        manager_id=None, work_email=f"{id_}@example.test", work_phone=None, slack_handle=None,
        timezone=None, employment_type=EmploymentType.fte, hire_date=date(2020, 1, 1),
        cost_centre=None, personal_mobile=None, availability_status=AvailabilityStatus.available,
        away_until=None, delegate_id=None, bio=None, photo_url=None, is_active=True,
    )
    emp.__dict__.update(overrides)
    db_session.add(emp)
    db_session.commit()
    return emp


def _skill_named(db_session, name: str) -> Skill | None:
    return db_session.query(Skill).filter(Skill.name.ilike(name)).first()


def _holding(db_session, employee_id: str, skill_name: str) -> EmployeeSkill | None:
    skill = _skill_named(db_session, skill_name)
    if skill is None:
        return None
    skill_id = skill.canonical_id or skill.id
    return (
        db_session.query(EmployeeSkill)
        .filter(EmployeeSkill.employee_id == employee_id, EmployeeSkill.skill_id == skill_id)
        .first()
    )


def _names(payload: dict, key: str) -> set[str]:
    return {item["name"] for item in payload.get(key, [])}


# ---------------------------------------------------------------------------
# 1. Identity, not role.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("role", ALL_ROLES)
async def test_any_role_adds_a_skill_to_their_own_profile(client, db_session, role):
    """The whole point: this is not a privileged capability. An ordinary
    employee gets exactly the same reach over their own record as hr does
    over theirs."""
    person = _mkemp(db_session, f"ownskill-add-{role}", f"Owner {role.title()}")
    res = await client.post(
        f"/people/{person.id}/skills",
        json={"skill": "Kubernetes", "category": "technical", "level": "Working"},
        headers=auth_headers(role, person.id),
    )
    assert res.status_code == 201, res.text
    assert "Kubernetes" in _names(res.json(), "skills")

    held = _holding(db_session, person.id, "Kubernetes")
    assert held is not None
    assert held.level is SkillLevel.working


@pytest.mark.parametrize("role", ALL_ROLES)
async def test_no_role_may_add_a_skill_to_someone_elses_profile(client, db_session, role):
    """hr and it included — running the directory is not a reason to put
    words in somebody's mouth about what they can do. This is the asymmetry
    a role-keyed gate would have inverted."""
    victim = _mkemp(db_session, "ownskill-victim", "Vic Victim")
    res = await client.post(
        f"/people/{victim.id}/skills",
        json={"skill": "Terraform", "category": "technical", "level": "Expert"},
        headers=auth_headers(role, f"ownskill-caller-{role}"),
    )
    assert res.status_code == 403
    assert _holding(db_session, victim.id, "Terraform") is None


@pytest.mark.parametrize("role", ALL_ROLES)
async def test_no_role_may_remove_someone_elses_skill(client, db_session, role):
    victim = _mkemp(db_session, "ownskill-victim-del", "Vera Victim")
    sre = _skill_named(db_session, "Site Reliability Engineering")
    if _holding(db_session, victim.id, "Site Reliability Engineering") is None:
        db_session.add(EmployeeSkill(
            employee_id=victim.id, skill_id=sre.id, level=SkillLevel.expert,
            source=SkillSource.confirmed, verified_at=datetime.now(),
        ))
        db_session.commit()

    res = await client.delete(
        f"/people/{victim.id}/skills",
        params={"skill": "Site Reliability Engineering"},
        headers=auth_headers(role, f"ownskill-caller-del-{role}"),
    )
    assert res.status_code == 403
    assert _holding(db_session, victim.id, "Site Reliability Engineering") is not None


async def test_employee_mode_does_not_disable_own_skill_edits(client, db_session):
    """No view_mode parameter exists on these routes at all, and that's
    deliberate: employee mode is the ordinary-colleague LENS, and an
    ordinary colleague editing their own profile is precisely what this
    endpoint is for. Asserting it here so a future "nothing is editable in
    employee mode" tidy-up doesn't quietly take self-service with it."""
    person = _mkemp(db_session, "ownskill-mode", "Mo Mode")
    res = await client.post(
        f"/people/{person.id}/skills?view_mode=employee",
        json={"skill": "Docker", "category": "technical", "level": "Learning"},
        headers=auth_headers("hr", person.id),
    )
    assert res.status_code == 201, res.text


# ---------------------------------------------------------------------------
# 2. Source: every write here is a self-claim, and says so.
# ---------------------------------------------------------------------------

async def test_added_skill_is_self_sourced_and_unverified(client, db_session):
    person = _mkemp(db_session, "ownskill-source", "Sol Source")
    res = await client.post(
        f"/people/{person.id}/skills",
        json={"skill": "Rust", "category": "technical", "level": "Expert"},
        headers=auth_headers("employee", person.id),
    )
    assert res.status_code == 201, res.text

    held = _holding(db_session, person.id, "Rust")
    # Expert IS allowed — source is what tells a reader this is a claim, so
    # capping the level would be solving with the wrong axis.
    assert held.level is SkillLevel.expert
    assert held.source is SkillSource.self_reported
    assert held.verified_at is None


async def test_relevelling_a_confirmed_skill_restamps_it_as_self_claimed(client, db_session):
    """The rule the whole source axis exists for: a self-set level must not
    inherit somebody else's verification."""
    person = _mkemp(db_session, "ownskill-restamp", "Ras Restamp")
    sre = _skill_named(db_session, "Site Reliability Engineering")
    if _holding(db_session, person.id, "Site Reliability Engineering") is None:
        db_session.add(EmployeeSkill(
            employee_id=person.id, skill_id=sre.id, level=SkillLevel.learning,
            source=SkillSource.confirmed, verified_at=datetime.now(),
        ))
        db_session.commit()

    res = await client.patch(
        f"/people/{person.id}/skills",
        json={"skill": "Site Reliability Engineering", "level": "Expert"},
        headers=auth_headers("employee", person.id),
    )
    assert res.status_code == 200, res.text

    db_session.expire_all()
    held = _holding(db_session, person.id, "Site Reliability Engineering")
    assert held.level is SkillLevel.expert
    assert held.source is SkillSource.self_reported
    assert held.verified_at is None


# ---------------------------------------------------------------------------
# 3. Skills and languages are one table; categories are enforced, not guessed.
# ---------------------------------------------------------------------------

async def test_language_lands_in_languages_not_skills(client, db_session):
    person = _mkemp(db_session, "ownskill-lang", "Lana Lang")
    res = await client.post(
        f"/people/{person.id}/skills",
        json={"skill": "French", "category": "language", "level": "Working"},
        headers=auth_headers("employee", person.id),
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert "French" in _names(body, "languages")
    assert "French" not in _names(body, "skills")


async def test_wrong_category_is_refused_rather_than_silently_refiled(client, db_session):
    """"French" submitted from the Skills card. Filing it under technical
    would re-categorise the skill for everyone who holds it; filing it under
    language anyway would make it vanish from the card the person typed it
    into. The error names the real category instead."""
    person = _mkemp(db_session, "ownskill-catmiss", "Cat Mismatch")
    res = await client.post(
        f"/people/{person.id}/skills",
        json={"skill": "French", "category": "technical", "level": "Working"},
        headers=auth_headers("employee", person.id),
    )
    assert res.status_code == 422
    assert "language" in res.json()["detail"]
    assert _holding(db_session, person.id, "French") is None
    # ...and the shared skill row keeps the category it had.
    assert _skill_named(db_session, "French").category is SkillCategory.language


async def test_domain_skill_added_from_the_skills_card_is_not_a_mismatch(client, db_session):
    """technical and domain render as one card — the profile never shows the
    category — so the Skills card sends "technical" for everything and a
    domain skill typed into it must land, not 422. Only the language split
    is visible, and therefore only the language split is enforced."""
    person = _mkemp(db_session, "ownskill-domain", "Dom Domain")
    db_session.add(Skill(name="Claims Adjudication", category=SkillCategory.domain, canonical_id=None))
    db_session.commit()

    res = await client.post(
        f"/people/{person.id}/skills",
        json={"skill": "Claims Adjudication", "category": "technical", "level": "Working"},
        headers=auth_headers("employee", person.id),
    )
    assert res.status_code == 201, res.text
    assert "Claims Adjudication" in _names(res.json(), "skills")
    # The shared skill row keeps its own category — this path never
    # re-categorises, it only decides whether to refuse.
    assert _skill_named(db_session, "Claims Adjudication").category is SkillCategory.domain


async def test_unknown_skill_name_is_created_under_the_requested_category(client, db_session):
    """Refusing unknown names would pin people to the seeded vocabulary.
    resolve_skill is the guard against sprawl, not refusal — see the next
    test."""
    person = _mkemp(db_session, "ownskill-new", "Nia New")
    res = await client.post(
        f"/people/{person.id}/skills",
        json={"skill": "Ancient Sumerian", "category": "language", "level": "Learning"},
        headers=auth_headers("employee", person.id),
    )
    assert res.status_code == 201, res.text
    assert "Ancient Sumerian" in _names(res.json(), "languages")
    assert _skill_named(db_session, "Ancient Sumerian").category is SkillCategory.language


async def test_synonym_attaches_to_the_canonical_skill(client, db_session):
    """"SRE" must not become a second, separate holding alongside Site
    Reliability Engineering — same rule app/proposals.py's _commit_skill
    follows on the review path."""
    person = _mkemp(db_session, "ownskill-synonym", "Sy Synonym")
    res = await client.post(
        f"/people/{person.id}/skills",
        json={"skill": "sre", "category": "technical", "level": "Working"},
        headers=auth_headers("employee", person.id),
    )
    assert res.status_code == 201, res.text
    assert "Site Reliability Engineering" in _names(res.json(), "skills")

    canonical = _skill_named(db_session, "Site Reliability Engineering")
    rows = (
        db_session.query(EmployeeSkill)
        .filter(EmployeeSkill.employee_id == person.id)
        .all()
    )
    assert [r.skill_id for r in rows] == [canonical.id]


# ---------------------------------------------------------------------------
# add / edit / remove, and the refusals around them.
# ---------------------------------------------------------------------------

async def test_add_edit_remove_round_trip(client, db_session):
    person = _mkemp(db_session, "ownskill-round", "Robin Round")
    headers = auth_headers("employee", person.id)

    added = await client.post(
        f"/people/{person.id}/skills",
        json={"skill": "Power BI", "category": "technical", "level": "Learning"},
        headers=headers,
    )
    assert added.status_code == 201, added.text
    assert "Power BI" in _names(added.json(), "skills")

    edited = await client.patch(
        f"/people/{person.id}/skills",
        json={"skill": "power bi", "level": "Expert"},  # case-insensitive, like every other lookup
        headers=headers,
    )
    assert edited.status_code == 200, edited.text
    db_session.expire_all()
    assert _holding(db_session, person.id, "Power BI").level is SkillLevel.expert

    removed = await client.delete(
        f"/people/{person.id}/skills", params={"skill": "Power BI"}, headers=headers,
    )
    assert removed.status_code == 200, removed.text
    assert "Power BI" not in _names(removed.json(), "skills")
    assert _holding(db_session, person.id, "Power BI") is None


async def test_adding_a_skill_twice_is_refused_not_silently_relevelled(client, db_session):
    """An "add" that overwrites is how a mistaken duplicate becomes data
    loss on the level that was already there — same reasoning
    app.writes.add_project_history refuses an existing membership."""
    person = _mkemp(db_session, "ownskill-dupe", "Dee Dupe")
    headers = auth_headers("employee", person.id)
    body = {"skill": "Terraform", "category": "technical", "level": "Expert"}

    assert (await client.post(f"/people/{person.id}/skills", json=body, headers=headers)).status_code == 201
    again = await client.post(
        f"/people/{person.id}/skills",
        json={**body, "level": "Learning"},
        headers=headers,
    )
    assert again.status_code == 409
    db_session.expire_all()
    assert _holding(db_session, person.id, "Terraform").level is SkillLevel.expert


async def test_editing_a_skill_you_dont_hold_is_404(client, db_session):
    person = _mkemp(db_session, "ownskill-missing", "Mel Missing")
    res = await client.patch(
        f"/people/{person.id}/skills",
        json={"skill": "Kubernetes", "level": "Expert"},
        headers=auth_headers("employee", person.id),
    )
    assert res.status_code == 404


async def test_removing_a_skill_you_dont_hold_is_404(client, db_session):
    person = _mkemp(db_session, "ownskill-missing-del", "Del Missing")
    res = await client.delete(
        f"/people/{person.id}/skills",
        params={"skill": "Nothing At All"},
        headers=auth_headers("employee", person.id),
    )
    assert res.status_code == 404


async def test_removing_leaves_the_shared_skill_row_for_everyone_else(client, db_session):
    """Only the holding goes. Other people hold the skill and project
    requirements point at it."""
    person = _mkemp(db_session, "ownskill-shared", "Shay Shared")
    sre = _skill_named(db_session, "Site Reliability Engineering")
    if _holding(db_session, person.id, "Site Reliability Engineering") is None:
        db_session.add(EmployeeSkill(
            employee_id=person.id, skill_id=sre.id, level=SkillLevel.working,
            source=SkillSource.self_reported, verified_at=None,
        ))
        db_session.commit()

    res = await client.delete(
        f"/people/{person.id}/skills",
        params={"skill": "Site Reliability Engineering"},
        headers=auth_headers("employee", person.id),
    )
    assert res.status_code == 200, res.text
    assert _skill_named(db_session, "Site Reliability Engineering") is not None
    # conftest's report-1 holds the same skill and is untouched.
    assert _holding(db_session, "report-1", "Site Reliability Engineering") is not None


async def test_slash_bearing_skill_names_survive_the_round_trip(client, db_session):
    """"CI/CD" and "Agile/Scrum" are real seeded skill names. A
    name-in-path endpoint would 404 on both — the slash is decoded back
    into a path separator before routing sees it — which is why PATCH takes
    the name in the body and DELETE in the query string."""
    person = _mkemp(db_session, "ownskill-slash", "Cy Slash")
    headers = auth_headers("employee", person.id)

    added = await client.post(
        f"/people/{person.id}/skills",
        json={"skill": "CI/CD", "category": "technical", "level": "Learning"},
        headers=headers,
    )
    assert added.status_code == 201, added.text

    edited = await client.patch(
        f"/people/{person.id}/skills", json={"skill": "CI/CD", "level": "Working"}, headers=headers,
    )
    assert edited.status_code == 200, edited.text

    removed = await client.delete(
        f"/people/{person.id}/skills", params={"skill": "CI/CD"}, headers=headers,
    )
    assert removed.status_code == 200, removed.text
    assert _holding(db_session, person.id, "CI/CD") is None


async def test_blank_and_overlong_names_are_rejected_by_validation(client, db_session):
    person = _mkemp(db_session, "ownskill-validate", "Val Validate")
    headers = auth_headers("employee", person.id)
    for skill in ["", "x" * 151]:
        res = await client.post(
            f"/people/{person.id}/skills",
            json={"skill": skill, "category": "technical", "level": "Working"},
            headers=headers,
        )
        assert res.status_code == 422, skill


async def test_unknown_level_is_rejected(client, db_session):
    person = _mkemp(db_session, "ownskill-level", "Lev Level")
    res = await client.post(
        f"/people/{person.id}/skills",
        json={"skill": "Kubernetes", "category": "technical", "level": "Grandmaster"},
        headers=auth_headers("employee", person.id),
    )
    assert res.status_code == 422


# ---------------------------------------------------------------------------
# Audit.
# ---------------------------------------------------------------------------

async def test_every_write_leaves_an_audit_row_naming_the_skill(client, db_session):
    """A removal in particular has to be on the record — a `confirmed`
    holding is removable here, so the audit row is what says who dropped
    it and what it was."""
    person = _mkemp(db_session, "ownskill-audit", "Aud Audit")
    headers = auth_headers("employee", person.id)

    await client.post(
        f"/people/{person.id}/skills",
        json={"skill": "Azure", "category": "technical", "level": "Learning"},
        headers=headers,
    )
    await client.patch(
        f"/people/{person.id}/skills", json={"skill": "Azure", "level": "Working"}, headers=headers,
    )
    await client.delete(f"/people/{person.id}/skills", params={"skill": "Azure"}, headers=headers)

    rows = (
        db_session.query(AuditLog)
        .filter(AuditLog.actor_id == person.id)
        .filter(AuditLog.action.in_(["add_own_skill", "update_own_skill", "remove_own_skill"]))
        .all()
    )
    assert {r.action for r in rows} == {"add_own_skill", "update_own_skill", "remove_own_skill"}
    assert all("Azure" in r.query_text for r in rows)
