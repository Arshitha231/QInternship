"""Community Graph: private per-employee "who to contact for what" edges.

Five structural guarantees this module exists to prove (per the feature
spec):

  1. An employee cannot edit/delete another employee's link, or an official
     link, even their own manager's.
  2. A mentor link flips to personal automatically after
     mentor_link_duration_days, and keeps working as an editable link after.
  3. Changing mentor_link_duration_days doesn't retroactively affect a link
     that already flipped under the old value.
  4. Official-link suggestions never auto-create a real edge, and confirming
     one doesn't overwrite an existing office/role mapping.
  5. GET /community_links never returns another employee's data, regardless
     of the requester's role (hr/it included).
"""
from datetime import date, datetime, timedelta

import pytest

from app.models import CommunityLink, Employee, Office, OrgSettings, OrgUnit
from app.models.enums import AvailabilityStatus, CommunityLinkSource, EmploymentType
from tests.conftest import auth_headers


@pytest.fixture
def org_wide_mentor_duration(db_session):
    """Exactly one org-wide OrgSettings row for the life of a test. Deleted
    afterward so it can't leak a stale duration into a later test sharing
    the same session-scoped database."""
    row = OrgSettings(office_id=None, mentor_link_duration_days=30)
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    yield row
    db_session.delete(row)
    db_session.commit()


@pytest.fixture
def payroll_office(db_session):
    """A dedicated office with one job-title signal (Payroll Specialist)
    and two ordinary employees, isolated from every other fixture office so
    the suggestion-generation tests can't be polluted by (or pollute) titles
    seeded elsewhere."""
    org_unit = db_session.query(OrgUnit).filter(OrgUnit.name == "Platform Engineering").one()
    office = Office(name="Suggestion Test Office", city="Suggestville", country="Testland", timezone="UTC")
    db_session.add(office)
    db_session.flush()

    def mk(id_, full_name, job_title) -> Employee:
        emp = Employee(
            id=id_, directory_object_id=None, full_name=full_name, preferred_name=None,
            job_title=job_title, org_unit_id=org_unit.id, office_id=office.id, manager_id=None,
            work_email=f"{id_}@example.test", work_phone=None, slack_handle=None, timezone=None,
            employment_type=EmploymentType.fte, hire_date=date(2021, 1, 1), cost_centre=None,
            personal_mobile=None, availability_status=AvailabilityStatus.available,
            away_until=None, delegate_id=None, bio=None, photo_url=None, is_active=True,
        )
        db_session.add(emp)
        return emp

    candidate = mk("suggest-payroll-1", "Pat Payroll", "Payroll Specialist")
    other1 = mk("suggest-other-1", "Ollie Other", "Software Engineer")
    other2 = mk("suggest-other-2", "Ozzy Other", "Software Engineer")
    db_session.commit()
    return {"office_id": office.id, "candidate_id": candidate.id, "other_ids": {other1.id, other2.id}}


def _mk_link(db_session, **overrides) -> CommunityLink:
    fields = dict(
        owner_employee_id="report-1", contact_employee_id="mgr-1", role_label="mentor",
        reason=None, source=CommunityLinkSource.official, office_id=None, department_id=None,
        is_mentor_link=False, created_at=datetime.now(),
    )
    fields.update(overrides)
    link = CommunityLink(**fields)
    db_session.add(link)
    db_session.commit()
    db_session.refresh(link)
    return link


# ---------------------------------------------------------------------------
# 1. Edit/delete permission: personal + owner only.
# ---------------------------------------------------------------------------

async def test_employee_cannot_edit_or_delete_someone_elses_personal_link(client):
    create_resp = await client.post(
        "/community_links",
        json={"contact_employee_id": "mgr-1", "role_label": "buddy", "reason": "helped me onboard"},
        headers=auth_headers("employee", "report-1"),
    )
    assert create_resp.status_code == 201, create_resp.text
    link_id = create_resp.json()["id"]

    # mgr-1 is report-1's own manager -- the relationship grants nothing here.
    patch_resp = await client.patch(
        f"/community_links/{link_id}", json={"role_label": "hijacked"},
        headers=auth_headers("manager", "mgr-1"),
    )
    assert patch_resp.status_code == 403

    delete_resp = await client.delete(f"/community_links/{link_id}", headers=auth_headers("manager", "mgr-1"))
    assert delete_resp.status_code == 403

    # A completely unrelated employee fares no better.
    patch_resp2 = await client.patch(
        f"/community_links/{link_id}", json={"role_label": "hijacked"},
        headers=auth_headers("employee", "stranger-1"),
    )
    assert patch_resp2.status_code == 403

    await client.delete(f"/community_links/{link_id}", headers=auth_headers("employee", "report-1"))


async def test_official_link_is_read_only_even_to_its_own_owner(client, db_session):
    link = _mk_link(db_session, role_label="payroll", source=CommunityLinkSource.official)

    patch_resp = await client.patch(
        f"/community_links/{link.id}", json={"role_label": "hr"},
        headers=auth_headers("employee", "report-1"),
    )
    assert patch_resp.status_code == 403

    delete_resp = await client.delete(f"/community_links/{link.id}", headers=auth_headers("employee", "report-1"))
    assert delete_resp.status_code == 403

    db_session.delete(link)
    db_session.commit()


# ---------------------------------------------------------------------------
# 2 & 3. Mentor-link expiration, computed at read time.
# ---------------------------------------------------------------------------

async def test_mentor_link_flips_to_personal_after_duration_and_stays_editable(
    client, db_session, org_wide_mentor_duration,
):
    link = _mk_link(
        db_session, role_label="mentor", source=CommunityLinkSource.official,
        is_mentor_link=True, created_at=datetime.now() - timedelta(days=40),  # duration is 30
    )

    resp = await client.get("/community_links", headers=auth_headers("employee", "report-1"))
    assert resp.status_code == 200
    flipped = next(r for r in resp.json() if r["id"] == link.id)
    assert flipped["source"] == "personal"

    db_session.expire_all()
    row = db_session.get(CommunityLink, link.id)
    assert row.source is CommunityLinkSource.personal

    # Now an ordinary editable personal link.
    patch_resp = await client.patch(
        f"/community_links/{link.id}", json={"role_label": "former mentor"},
        headers=auth_headers("employee", "report-1"),
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["role_label"] == "former mentor"

    await client.delete(f"/community_links/{link.id}", headers=auth_headers("employee", "report-1"))


async def test_mentor_link_not_yet_expired_stays_official(client, db_session, org_wide_mentor_duration):
    link = _mk_link(
        db_session, role_label="mentor", source=CommunityLinkSource.official,
        is_mentor_link=True, created_at=datetime.now() - timedelta(days=5),  # duration is 30
    )

    resp = await client.get("/community_links", headers=auth_headers("employee", "report-1"))
    row = next(r for r in resp.json() if r["id"] == link.id)
    assert row["source"] == "official"

    # Still read-only, being official.
    patch_resp = await client.patch(
        f"/community_links/{link.id}", json={"role_label": "x"},
        headers=auth_headers("employee", "report-1"),
    )
    assert patch_resp.status_code == 403

    db_session.delete(link)
    db_session.commit()


async def test_raising_mentor_duration_does_not_unflip_an_already_expired_link(
    client, db_session, org_wide_mentor_duration,
):
    link = _mk_link(
        db_session, role_label="mentor", source=CommunityLinkSource.official,
        is_mentor_link=True, created_at=datetime.now() - timedelta(days=40),  # duration is 30
    )

    first = await client.get("/community_links", headers=auth_headers("employee", "report-1"))
    assert next(r for r in first.json() if r["id"] == link.id)["source"] == "personal"

    # HR raises the org-wide duration well past this link's age.
    org_wide_mentor_duration.mentor_link_duration_days = 99999
    db_session.commit()

    second = await client.get("/community_links", headers=auth_headers("employee", "report-1"))
    still = next(r for r in second.json() if r["id"] == link.id)
    assert still["source"] == "personal"  # stays flipped, never reverted

    await client.delete(f"/community_links/{link.id}", headers=auth_headers("employee", "report-1"))


# ---------------------------------------------------------------------------
# 4. Official-link suggestions: staged, never auto-created, never overwritten.
# ---------------------------------------------------------------------------

async def test_suggestions_never_auto_create_and_confirm_does_not_overwrite(client, db_session, payroll_office):
    office_id = payroll_office["office_id"]
    candidate_id = payroll_office["candidate_id"]

    gen_resp = await client.post("/suggested_official_links/generate", headers=auth_headers("hr", "hr-gen-1"))
    assert gen_resp.status_code == 201, gen_resp.text
    staged = [s for s in gen_resp.json() if s["office_id"] == office_id and s["role_label"] == "payroll"]
    assert len(staged) == 1
    assert staged[0]["candidate_employee_id"] == candidate_id
    assert staged[0]["status"] == "pending"

    # Generation alone must never create a real edge.
    assert db_session.query(CommunityLink).filter(CommunityLink.office_id == office_id).count() == 0

    confirm_resp = await client.post(
        f"/suggested_official_links/{staged[0]['id']}/confirm", headers=auth_headers("hr", "hr-gen-1"))
    assert confirm_resp.status_code == 200
    assert confirm_resp.json()["status"] == "confirmed"

    db_session.expire_all()
    links = (
        db_session.query(CommunityLink)
        .filter(CommunityLink.office_id == office_id, CommunityLink.role_label == "payroll",
                CommunityLink.source == CommunityLinkSource.official)
        .all()
    )
    assert {row.owner_employee_id for row in links} == payroll_office["other_ids"]
    assert all(row.contact_employee_id == candidate_id for row in links)

    # Re-generating must not re-stage the now-mapped office/role.
    gen_resp2 = await client.post("/suggested_official_links/generate", headers=auth_headers("hr", "hr-gen-1"))
    assert gen_resp2.status_code == 201
    assert [
        s for s in gen_resp2.json() if s["office_id"] == office_id and s["role_label"] == "payroll"
    ] == []

    # And nothing was overwritten -- same link count as after the first confirm.
    assert (
        db_session.query(CommunityLink)
        .filter(CommunityLink.office_id == office_id, CommunityLink.role_label == "payroll")
        .count()
        == len(payroll_office["other_ids"])
    )


async def test_generating_and_reviewing_suggestions_is_hr_only(client):
    gen_resp = await client.post("/suggested_official_links/generate", headers=auth_headers("it", "it-1"))
    assert gen_resp.status_code == 403

    list_resp = await client.get("/suggested_official_links", headers=auth_headers("manager", "mgr-1"))
    assert list_resp.status_code == 403


# ---------------------------------------------------------------------------
# 5. GET /community_links never returns another employee's data.
# ---------------------------------------------------------------------------

async def test_get_community_links_never_returns_another_employees_data(client):
    create_resp = await client.post(
        "/community_links",
        json={"contact_employee_id": "mgr-1", "role_label": "buddy", "reason": "friendly face"},
        headers=auth_headers("employee", "report-1"),
    )
    assert create_resp.status_code == 201
    link_id = create_resp.json()["id"]

    for role, user_id in [
        ("employee", "stranger-1"),
        ("manager", "mgr-1"),  # report-1's own manager
        ("hr", "hr-snoop-1"),
        ("it", "it-snoop-1"),
    ]:
        resp = await client.get("/community_links", headers=auth_headers(role, user_id))
        assert resp.status_code == 200
        body = resp.json()
        assert link_id not in {row["id"] for row in body}
        assert "report-1" not in {row["owner_employee_id"] for row in body}

    # report-1 sees their own link, of course.
    own = await client.get("/community_links", headers=auth_headers("employee", "report-1"))
    assert link_id in {row["id"] for row in own.json()}


# ---------------------------------------------------------------------------
# 6. Manager: surfaced from org data (Employee.manager_id), never a stored
#    community_links row.
# ---------------------------------------------------------------------------

async def test_manager_is_surfaced_without_a_stored_row(client, db_session):
    # report-1's manager is mgr-1, per conftest's seed -- no community_links
    # row involved at all.
    before = db_session.query(CommunityLink).filter(CommunityLink.owner_employee_id == "report-1").count()

    resp = await client.get("/community_links", headers=auth_headers("employee", "report-1"))
    assert resp.status_code == 200
    manager_rows = [r for r in resp.json() if r["role_label"] == "manager"]
    assert len(manager_rows) == 1
    row = manager_rows[0]
    assert row["contact_employee_id"] == "mgr-1"
    assert row["source"] == "official"

    # Nothing was written to prove it.
    after = db_session.query(CommunityLink).filter(CommunityLink.owner_employee_id == "report-1").count()
    assert after == before


async def test_manager_entry_is_read_only_and_not_a_real_row(client):
    resp = await client.get("/community_links", headers=auth_headers("employee", "report-1"))
    manager_id = next(r["id"] for r in resp.json() if r["role_label"] == "manager")

    patch_resp = await client.patch(
        f"/community_links/{manager_id}", json={"role_label": "x"},
        headers=auth_headers("employee", "report-1"),
    )
    assert patch_resp.status_code == 404  # no such row -- there never was one

    delete_resp = await client.delete(f"/community_links/{manager_id}", headers=auth_headers("employee", "report-1"))
    assert delete_resp.status_code == 404


async def test_no_manager_entry_when_employee_has_none_on_file(client):
    # conf-owner-1 has no manager_id in the seed fixtures.
    resp = await client.get("/community_links", headers=auth_headers("employee", "conf-owner-1"))
    assert resp.status_code == 200
    assert [r for r in resp.json() if r["role_label"] == "manager"] == []


# ---------------------------------------------------------------------------
# 7. Expanded official-role keyword categories: benefits, facilities,
#    security/compliance -- same job-title-substring mechanism as payroll.
# ---------------------------------------------------------------------------

@pytest.fixture
def role_coverage_office(db_session):
    """A dedicated office covering the newer keyword-derived roles, isolated
    from every other fixture office for the same reason payroll_office is."""
    org_unit = db_session.query(OrgUnit).filter(OrgUnit.name == "Finance Operations").one()
    office = Office(name="Role Coverage Office", city="Coverville", country="Testland", timezone="UTC")
    db_session.add(office)
    db_session.flush()

    def mk(id_, full_name, job_title) -> Employee:
        emp = Employee(
            id=id_, directory_object_id=None, full_name=full_name, preferred_name=None,
            job_title=job_title, org_unit_id=org_unit.id, office_id=office.id, manager_id=None,
            work_email=f"{id_}@example.test", work_phone=None, slack_handle=None, timezone=None,
            employment_type=EmploymentType.fte, hire_date=date(2021, 1, 1), cost_centre=None,
            personal_mobile=None, availability_status=AvailabilityStatus.available,
            away_until=None, delegate_id=None, bio=None, photo_url=None, is_active=True,
        )
        db_session.add(emp)
        return emp

    benefits = mk("suggest-benefits-1", "Bo Benefits", "Benefits Administrator")
    facilities = mk("suggest-facilities-1", "Fran Facilities", "Office Manager")
    security = mk("suggest-security-1", "Sam Security", "Security & Compliance Lead")
    db_session.commit()
    return {"office_id": office.id, "benefits_id": benefits.id, "facilities_id": facilities.id, "security_id": security.id}


async def test_new_role_keywords_generate_suggestions(client, role_coverage_office):
    gen_resp = await client.post("/suggested_official_links/generate", headers=auth_headers("hr", "hr-gen-2"))
    assert gen_resp.status_code == 201, gen_resp.text
    office_id = role_coverage_office["office_id"]

    by_role = {
        s["role_label"]: s["candidate_employee_id"]
        for s in gen_resp.json() if s["office_id"] == office_id
    }
    assert by_role.get("benefits_admin") == role_coverage_office["benefits_id"]
    assert by_role.get("facilities_admin") == role_coverage_office["facilities_id"]
    assert by_role.get("security_compliance") == role_coverage_office["security_id"]


# ---------------------------------------------------------------------------
# 8. Mentor auto-assignment for new hires -- HR-only, direct creation (no
#    suggest/confirm step), idempotent, and time-boxed via the same
#    is_mentor_link expiration every other mentor link already uses.
# ---------------------------------------------------------------------------

@pytest.fixture
def new_hire_team(db_session):
    """One long-tenured colleague and one brand-new hire in the same org
    unit and office. A dedicated OrgUnit, not a shared one like
    "Platform Engineering" -- the mentor sweep's team-first pairing picks
    the most recently hired ELIGIBLE colleague across the whole org unit,
    so reusing a fixture unit dozens of other seeded employees (including
    some hired more recently than "senior") already belong to would make
    the "right" candidate nondeterministic instead of the fixture's two
    employees being the only members."""
    company = db_session.query(OrgUnit).filter(OrgUnit.unit_type == "company").one()
    org_unit = OrgUnit(name="Mentor Sweep Team", parent_id=company.id, unit_type="team")
    db_session.add(org_unit)
    office = Office(name="Mentor Sweep Office", city="Mentorville", country="Testland", timezone="UTC")
    db_session.add(office)
    db_session.flush()

    def mk(id_, full_name, hire_date_, **overrides) -> Employee:
        fields = dict(
            id=id_, directory_object_id=None, full_name=full_name, preferred_name=None,
            job_title="Software Engineer", org_unit_id=org_unit.id, office_id=office.id, manager_id=None,
            work_email=f"{id_}@example.test", work_phone=None, slack_handle=None, timezone=None,
            employment_type=EmploymentType.fte, hire_date=hire_date_, cost_centre=None,
            personal_mobile=None, availability_status=AvailabilityStatus.available,
            away_until=None, delegate_id=None, bio=None, photo_url=None, is_active=True,
        )
        fields.update(overrides)
        emp = Employee(**fields)
        db_session.add(emp)
        return emp

    senior = mk("mentor-senior-1", "Sasha Senior", date(2019, 1, 1))
    new_hire = mk("mentor-new-1", "Nico Newhire", date.today() - timedelta(days=10))
    db_session.commit()
    return {"senior_id": senior.id, "new_hire_id": new_hire.id}


async def test_auto_assign_mentors_pairs_new_hire_and_is_idempotent(client, db_session, new_hire_team):
    resp = await client.post("/community_links/auto_assign_mentors", headers=auth_headers("hr", "hr-mentor-1"))
    assert resp.status_code == 201, resp.text
    created = [r for r in resp.json() if r["owner_employee_id"] == new_hire_team["new_hire_id"]]
    assert len(created) == 1
    assert created[0]["contact_employee_id"] == new_hire_team["senior_id"]
    assert created[0]["role_label"] == "mentor"
    assert created[0]["source"] == "official"
    assert created[0]["is_mentor_link"] is True

    # Re-running must not create a second mentor link for the same person.
    resp2 = await client.post("/community_links/auto_assign_mentors", headers=auth_headers("hr", "hr-mentor-1"))
    assert resp2.status_code == 201
    assert [r for r in resp2.json() if r["owner_employee_id"] == new_hire_team["new_hire_id"]] == []

    db_session.expire_all()
    count = (
        db_session.query(CommunityLink)
        .filter(CommunityLink.owner_employee_id == new_hire_team["new_hire_id"], CommunityLink.is_mentor_link == True)  # noqa: E712
        .count()
    )
    assert count == 1


async def test_auto_assign_mentors_skips_someone_with_an_already_expired_mentor_link(
    client, db_session, org_wide_mentor_duration,
):
    # A mentor link that already flipped to personal (source=personal,
    # is_mentor_link still True) counts as "already had one assigned" --
    # the sweep must not top it up with a second.
    already = CommunityLink(
        owner_employee_id="report-1", contact_employee_id="mgr-1", role_label="mentor",
        reason=None, source=CommunityLinkSource.personal, office_id=None, department_id=None,
        is_mentor_link=True, created_at=datetime.now() - timedelta(days=200),
    )
    db_session.add(already)
    # report-1 needs a recent-enough hire_date to otherwise qualify as a new
    # hire -- prove the skip is about the existing link, not the date.
    report = db_session.get(Employee, "report-1")
    original_hire_date = report.hire_date
    report.hire_date = date.today() - timedelta(days=5)
    db_session.commit()

    try:
        resp = await client.post("/community_links/auto_assign_mentors", headers=auth_headers("hr", "hr-mentor-2"))
        assert resp.status_code == 201
        assert [r for r in resp.json() if r["owner_employee_id"] == "report-1"] == []
    finally:
        db_session.delete(already)
        report.hire_date = original_hire_date
        db_session.commit()


async def test_auto_assign_mentors_is_hr_only(client):
    resp = await client.post("/community_links/auto_assign_mentors", headers=auth_headers("manager", "mgr-1"))
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# 9. The keyword table vs. the titles that actually exist.
#
# Every other test in this file builds its own fixture employees, which makes
# them USELESS as a check on the keyword sets: the fixtures above were
# written with titles picked to match the keywords ("Benefits Administrator",
# "Office Manager"), so they passed while the real directory contained zero
# employees matching either -- facilities_admin and benefits_admin were dead
# paths that could never stage a suggestion, and no test noticed. A role
# whose keywords match nobody is indistinguishable, at the API level, from a
# role the company genuinely doesn't have: both just silently return nothing.
#
# So the guard has to be against a REAL source of titles rather than a
# fixture. seed_workplace_roles.py is that source for the two roles that only
# exist because it hires them -- its title constants and the keyword table
# are a genuine coupling, and renaming either side without the other empties
# the role again.
# ---------------------------------------------------------------------------

def test_keyword_table_matches_the_titles_the_seed_script_actually_creates():
    import seed_workplace_roles

    from app.community_links import _TITLE_KEYWORD_ROLES

    def roles_matching(title: str) -> list[str]:
        lowered = title.lower()
        return sorted(
            role for role, keywords in _TITLE_KEYWORD_ROLES.items()
            if any(keyword in lowered for keyword in keywords)
        )

    assert "facilities_admin" in roles_matching(seed_workplace_roles.FACILITIES_TITLE)
    assert "benefits_admin" in roles_matching(seed_workplace_roles.BENEFITS_TITLE)


def test_access_request_titles_resolve_to_the_security_contact():
    """The spec names access requests as the reason this role exists, and in
    this directory those are owned by the Identity & Access titles -- not by
    anything containing "security" or "compliance", which is all the first
    version of the keyword set looked for (it missed all ten of them)."""
    from app.community_links import _TITLE_KEYWORD_ROLES

    keywords = _TITLE_KEYWORD_ROLES["security_compliance"]
    for title in ("Identity & Access Analyst", "Identity & Endpoints Team Manager",
                  "Senior Compliance Analyst"):
        assert any(k in title.lower() for k in keywords), title


# ---------------------------------------------------------------------------
# 6. A link outlives the person it points at, but stops being rendered.
#
# Nothing deletes community_links when somebody is deactivated or restricted,
# and nothing should — the row is the owner's own note about who to ask for
# what, and it should come back intact if that person returns. But the link
# is only a pointer, and the graph used to render one whose contact had since
# become invisible: the frontend's per-contact profile lookup 404'd, and the
# card fell back to printing the raw contact id with a green "available" dot.
#
# So the list filters on whether the contact is still visible to this caller,
# using the same is_active + obligations pair every other read path uses.
# ---------------------------------------------------------------------------

def _mk_contact(db_session, id_, full_name, **overrides) -> Employee:
    org_unit = db_session.query(OrgUnit).filter(OrgUnit.name == "Platform Engineering").one()
    fields = dict(
        id=id_, directory_object_id=None, full_name=full_name, preferred_name=None,
        job_title="Engineer", org_unit_id=org_unit.id, office_id=None, manager_id=None,
        work_email=f"{id_}@example.test", work_phone=None, slack_handle=None, timezone=None,
        employment_type=EmploymentType.fte, hire_date=date(2021, 1, 1), cost_centre=None,
        personal_mobile=None, availability_status=AvailabilityStatus.available,
        away_until=None, delegate_id=None, bio=None, photo_url=None, is_active=True,
    )
    fields.update(overrides)
    emp = Employee(**fields)
    db_session.add(emp)
    db_session.commit()
    return emp


async def test_link_to_a_deactivated_contact_is_omitted(client, db_session):
    owner = _mk_contact(db_session, "cl-vis-owner-1", "Auben Keeper")
    gone = _mk_contact(db_session, "cl-vis-gone", "Tobin Departed")
    _mk_link(db_session, owner_employee_id=owner.id, contact_employee_id=gone.id,
             source=CommunityLinkSource.personal, role_label="expenses")

    before = await client.get("/community_links", headers=auth_headers("employee", owner.id))
    assert [r["contact_employee_id"] for r in before.json()] == [gone.id]

    gone.is_active = False
    db_session.commit()

    after = await client.get("/community_links", headers=auth_headers("employee", owner.id))
    assert after.status_code == 200
    assert after.json() == []


async def test_link_to_a_restricted_contact_is_omitted(client, db_session):
    owner = _mk_contact(db_session, "cl-vis-owner-2", "Brice Keeper")
    hidden = _mk_contact(db_session, "cl-vis-hidden", "Wynne Unseen")
    _mk_link(db_session, owner_employee_id=owner.id, contact_employee_id=hidden.id,
             source=CommunityLinkSource.personal, role_label="payroll")

    hidden.availability_status = AvailabilityStatus.restricted
    db_session.commit()

    resp = await client.get("/community_links", headers=auth_headers("employee", owner.id))
    assert resp.status_code == 200
    assert resp.json() == []


async def test_the_row_survives_and_the_link_returns_when_the_contact_does(client, db_session):
    """Filtered at read time, not deleted — the distinction that makes this
    safe. Reactivating the contact restores the owner's link untouched."""
    owner = _mk_contact(db_session, "cl-vis-owner-3", "Cato Keeper")
    contact = _mk_contact(db_session, "cl-vis-returner", "Ines Comeback")
    link = _mk_link(db_session, owner_employee_id=owner.id, contact_employee_id=contact.id,
                    source=CommunityLinkSource.personal, role_label="onboarding buddy")

    contact.is_active = False
    db_session.commit()
    gone = await client.get("/community_links", headers=auth_headers("employee", owner.id))
    assert gone.json() == []

    contact.is_active = True
    db_session.commit()

    resp = await client.get("/community_links", headers=auth_headers("employee", owner.id))
    rows = resp.json()
    assert [r["id"] for r in rows] == [link.id]
    assert rows[0]["role_label"] == "onboarding buddy"
    assert db_session.get(CommunityLink, link.id) is not None


async def test_hr_in_work_mode_still_sees_a_restricted_contact(client, db_session):
    """The filter is the ordinary visibility rule, not a new one — so hr in
    work mode keeps its exemption here exactly as it does on a profile."""
    owner = _mk_contact(db_session, "cl-vis-hr-owner", "Delphine Keeper")
    hidden = _mk_contact(db_session, "cl-vis-hr-hidden", "Marlowe Quiet",
                         availability_status=AvailabilityStatus.restricted)
    _mk_link(db_session, owner_employee_id=owner.id, contact_employee_id=hidden.id,
             source=CommunityLinkSource.personal, role_label="investigations")

    work = await client.get(
        "/community_links", params={"view_mode": "work"}, headers=auth_headers("hr", owner.id))
    assert [r["contact_employee_id"] for r in work.json()] == [hidden.id]

    # ...and loses it in employee mode, same as everywhere else. This is the
    # case a work-mode-only filter would have got wrong: the link list would
    # return a contact that GET /people/{id} in the same mode refuses.
    employee_mode = await client.get(
        "/community_links", params={"view_mode": "employee"}, headers=auth_headers("hr", owner.id))
    assert employee_mode.json() == []


async def test_synthesized_manager_entry_drops_a_restricted_manager(client, db_session):
    """The manager entry is derived from org data rather than stored, so it
    needed the same check applied separately — it only tested is_active."""
    boss = _mk_contact(db_session, "cl-vis-boss", "Clemence Uphill")
    underling = _mk_contact(db_session, "cl-vis-underling", "Devan Downhill", manager_id=boss.id)

    before = await client.get("/community_links", headers=auth_headers("employee", underling.id))
    assert [r["role_label"] for r in before.json()] == ["manager"]

    boss.availability_status = AvailabilityStatus.restricted
    db_session.commit()

    after = await client.get("/community_links", headers=auth_headers("employee", underling.id))
    assert after.json() == []
