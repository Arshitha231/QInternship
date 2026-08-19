"""IT, work mode: editing anyone's project history EXCEPT their own.

Two rules, tested independently because they fail independently:

  1. IT can create, correct and remove any employee's EmployeeProject row
     directly — without a document, which is the only route that existed
     before (app/proposals.py's accept/edit committing a proposed_change).
  2. IT cannot do any of it to themselves, through either route. The direct
     endpoints refuse it, and so does the review pipeline — which had no
     such check at all, so an IT reviewer could upload a document about
     themselves and accept it onto their own profile.

Every denial below calls the endpoint directly with whatever role and id it
likes: there is no preceding read to have been filtered and no frontend to
have hidden a button, same standard as tests/test_write_endpoints.py.
"""
from datetime import date

import pytest

from app.models import AuditLog, Employee, EmployeeProject, Office, OrgUnit, Project
from app.models.enums import (
    AvailabilityStatus, EmploymentType, ProjectClassification, ProjectType,
)
from tests.conftest import auth_headers

ALL_ROLES = ["employee", "manager", "hr", "it"]


@pytest.fixture
def atlas_id(db_session):
    """A project dedicated to THIS module, not conftest's shared "Project
    Atlas". Every test here adds memberships, and the test database is
    session-scoped — piling members onto a shared project leaks into
    whatever runs next (it broke test_project_search's restricted-employee
    hop, which counts who is on a project). Same reasoning _mkemp already
    applies to people, applied to projects."""
    existing = (
        db_session.query(Project)
        .filter(Project.name == "Project HistoryFixture").first()
    )
    if existing is not None:
        return existing.id
    owner = db_session.query(Employee).filter(Employee.is_active == True).first()  # noqa: E712
    project = Project(
        name="Project HistoryFixture", type=ProjectType.project,
        description="Fixture project for project-history write tests.",
        owning_unit_id=owner.org_unit_id, owner_id=owner.id,
        classification=ProjectClassification.internal, is_client_engagement=False,
    )
    db_session.add(project)
    db_session.commit()
    return project.id


def _mkemp(db_session, id_, full_name, **overrides) -> Employee:
    """Throwaway employee per test — never a shared conftest fixture
    person, since the test database is session-scoped."""
    office = db_session.query(Office).filter(Office.name == "Test HQ").one()
    org_unit = db_session.query(OrgUnit).filter(OrgUnit.name == "Platform Engineering").one()
    fields = dict(
        id=id_, full_name=full_name, job_title="Test Employee",
        org_unit_id=org_unit.id, office_id=office.id, manager_id=None,
        work_email=f"{id_}@example.test", employment_type=EmploymentType.fte,
        hire_date=date(2021, 1, 1), availability_status=AvailabilityStatus.available,
        is_active=True,
    )
    fields.update(overrides)
    emp = Employee(**fields)
    db_session.add(emp)
    db_session.commit()
    return emp


def _membership(db_session, employee_id, project_id) -> EmployeeProject | None:
    db_session.expire_all()
    return (
        db_session.query(EmployeeProject)
        .filter(EmployeeProject.employee_id == employee_id,
                EmployeeProject.project_id == project_id)
        .first()
    )


# ---------------------------------------------------------------------------
# The new capability: edit anyone's project history.
# ---------------------------------------------------------------------------

async def test_it_creates_a_project_membership(client, db_session, atlas_id):
    _mkemp(db_session, "ph-create-1", "Pat Create")
    resp = await client.put(
        f"/people/ph-create-1/projects/{atlas_id}", params={"view_mode": "work"},
        json={"role": "Platform Lead", "start_date": "2025-02-01",
              "contribution": "Owned the cutover plan."},
        headers=auth_headers("it", "it-writer-1"),
    )
    assert resp.status_code == 200, resp.text

    row = _membership(db_session, "ph-create-1", atlas_id)
    assert row is not None
    assert row.role == "Platform Lead"
    assert row.start_date == date(2025, 2, 1)
    assert row.contribution == "Owned the cutover plan."
    assert row.end_date is None


async def test_it_patches_only_the_supplied_keys(client, db_session, atlas_id):
    """PATCH semantics on an existing row, same contract as
    update_employee: an omitted key is untouched, not reset."""
    _mkemp(db_session, "ph-patch-1", "Pat Patch")
    await client.put(
        f"/people/ph-patch-1/projects/{atlas_id}", params={"view_mode": "work"},
        json={"role": "Engineer", "start_date": "2024-01-01",
              "contribution": "Original prose."},
        headers=auth_headers("it"),
    )
    resp = await client.put(
        f"/people/ph-patch-1/projects/{atlas_id}", params={"view_mode": "work"},
        json={"role": "Senior Engineer"}, headers=auth_headers("it"),
    )
    assert resp.status_code == 200, resp.text

    row = _membership(db_session, "ph-patch-1", atlas_id)
    assert row.role == "Senior Engineer"
    assert row.contribution == "Original prose."      # untouched
    assert row.start_date == date(2024, 1, 1)          # untouched


async def test_explicit_null_end_date_makes_a_project_current_again(
    client, db_session, atlas_id
):
    """The distinction the PATCH-with-partial-dict contract exists for:
    null clears, omission leaves alone."""
    _mkemp(db_session, "ph-null-1", "Pat Null")
    await client.put(
        f"/people/ph-null-1/projects/{atlas_id}", params={"view_mode": "work"},
        json={"role": "Engineer", "start_date": "2024-01-01", "end_date": "2024-09-01"},
        headers=auth_headers("it"),
    )
    assert _membership(db_session, "ph-null-1", atlas_id).end_date == date(2024, 9, 1)

    resp = await client.put(
        f"/people/ph-null-1/projects/{atlas_id}", params={"view_mode": "work"},
        json={"end_date": None}, headers=auth_headers("it"),
    )
    assert resp.status_code == 200, resp.text
    assert _membership(db_session, "ph-null-1", atlas_id).end_date is None


async def test_repeating_the_put_converges_on_one_row(client, db_session, atlas_id):
    """(person, project) identifies the membership, so PUT is idempotent —
    it must never stack duplicate rows for the same pair."""
    _mkemp(db_session, "ph-idem-1", "Pat Idem")
    for _ in range(3):
        await client.put(
            f"/people/ph-idem-1/projects/{atlas_id}", params={"view_mode": "work"},
            json={"role": "Engineer", "start_date": "2024-01-01"},
            headers=auth_headers("it"),
        )
    db_session.expire_all()
    rows = (
        db_session.query(EmployeeProject)
        .filter(EmployeeProject.employee_id == "ph-idem-1",
                EmployeeProject.project_id == atlas_id).all()
    )
    assert len(rows) == 1


async def test_it_removes_a_project_membership(client, db_session, atlas_id):
    _mkemp(db_session, "ph-del-1", "Pat Delete")
    await client.put(
        f"/people/ph-del-1/projects/{atlas_id}", params={"view_mode": "work"},
        json={"role": "Engineer", "start_date": "2024-01-01"}, headers=auth_headers("it"),
    )
    assert _membership(db_session, "ph-del-1", atlas_id) is not None

    resp = await client.delete(
        f"/people/ph-del-1/projects/{atlas_id}", params={"view_mode": "work"},
        headers=auth_headers("it"),
    )
    assert resp.status_code == 204, resp.text
    assert _membership(db_session, "ph-del-1", atlas_id) is None

    # The project itself survives — this removes a membership, not a project.
    assert db_session.get(Project, atlas_id) is not None


async def test_creating_requires_role_and_start_date(client, db_session, atlas_id):
    """Both are NOT NULL on EmployeeProject and there's no document here to
    default them from, so a create that omits them is a 422, not a row with
    invented values."""
    _mkemp(db_session, "ph-req-1", "Pat Required")
    resp = await client.put(
        f"/people/ph-req-1/projects/{atlas_id}", params={"view_mode": "work"},
        json={"contribution": "prose only"}, headers=auth_headers("it"),
    )
    assert resp.status_code == 422, resp.text
    assert _membership(db_session, "ph-req-1", atlas_id) is None


async def test_end_date_before_start_date_is_refused(client, db_session, atlas_id):
    _mkemp(db_session, "ph-order-1", "Pat Order")
    resp = await client.put(
        f"/people/ph-order-1/projects/{atlas_id}", params={"view_mode": "work"},
        json={"role": "Engineer", "start_date": "2024-06-01", "end_date": "2024-01-01"},
        headers=auth_headers("it"),
    )
    assert resp.status_code == 422, resp.text


async def test_unknown_employee_or_project_is_404(client, db_session, atlas_id):
    _mkemp(db_session, "ph-404-1", "Pat Missing")
    missing_person = await client.put(
        f"/people/nobody-at-all/projects/{atlas_id}", params={"view_mode": "work"},
        json={"role": "Engineer", "start_date": "2024-01-01"}, headers=auth_headers("it"),
    )
    assert missing_person.status_code == 404

    missing_project = await client.put(
        "/people/ph-404-1/projects/999999", params={"view_mode": "work"},
        json={"role": "Engineer", "start_date": "2024-01-01"}, headers=auth_headers("it"),
    )
    assert missing_project.status_code == 404


async def test_write_is_audited(client, db_session, atlas_id):
    _mkemp(db_session, "ph-audit-1", "Pat Audit")
    await client.put(
        f"/people/ph-audit-1/projects/{atlas_id}", params={"view_mode": "work"},
        json={"role": "Engineer", "start_date": "2024-01-01"},
        headers=auth_headers("it", "it-auditor-1"),
    )
    rows = (
        db_session.query(AuditLog)
        .filter(AuditLog.actor_id == "it-auditor-1",
                AuditLog.action == "create_project_history").all()
    )
    assert len(rows) == 1
    assert "ph-audit-1" in rows[0].query_text


# ---------------------------------------------------------------------------
# The exclusion: except their own.
# ---------------------------------------------------------------------------

async def test_it_cannot_edit_their_own_project_history(client, db_session, atlas_id):
    """The rule the whole feature is scoped by. Same shape as
    update_employee's "an hr caller giving themselves a raise"."""
    _mkemp(db_session, "ph-self-1", "Pat Self")
    resp = await client.put(
        f"/people/ph-self-1/projects/{atlas_id}", params={"view_mode": "work"},
        json={"role": "Principal Engineer", "start_date": "2024-01-01"},
        headers=auth_headers("it", "ph-self-1"),
    )
    assert resp.status_code == 403, resp.text
    assert _membership(db_session, "ph-self-1", atlas_id) is None


async def test_it_cannot_remove_their_own_project_history(client, db_session, atlas_id):
    _mkemp(db_session, "ph-self-2", "Pat Self Two")
    await client.put(
        f"/people/ph-self-2/projects/{atlas_id}", params={"view_mode": "work"},
        json={"role": "Engineer", "start_date": "2024-01-01"},
        headers=auth_headers("it", "it-someone-else"),
    )
    resp = await client.delete(
        f"/people/ph-self-2/projects/{atlas_id}", params={"view_mode": "work"},
        headers=auth_headers("it", "ph-self-2"),
    )
    assert resp.status_code == 403, resp.text
    assert _membership(db_session, "ph-self-2", atlas_id) is not None


@pytest.mark.parametrize("role", [r for r in ALL_ROLES if r != "it"])
async def test_only_it_may_edit_project_history(client, db_session, atlas_id, role):
    _mkemp(db_session, f"ph-role-{role}", f"Pat {role}")
    resp = await client.put(
        f"/people/ph-role-{role}/projects/{atlas_id}", params={"view_mode": "work"},
        json={"role": "Engineer", "start_date": "2024-01-01"}, headers=auth_headers(role),
    )
    assert resp.status_code == 403, resp.text


async def test_it_cannot_edit_project_history_in_employee_mode(
    client, db_session, atlas_id
):
    """EDITABLE[("it", "employee")] is empty — the capability is work-mode
    only, and asking for employee mode must not be a way around that."""
    _mkemp(db_session, "ph-mode-1", "Pat Mode")
    resp = await client.put(
        f"/people/ph-mode-1/projects/{atlas_id}", params={"view_mode": "employee"},
        json={"role": "Engineer", "start_date": "2024-01-01"}, headers=auth_headers("it"),
    )
    assert resp.status_code == 403, resp.text


async def test_identifying_columns_are_not_editable(client, db_session, atlas_id):
    """employee_id/project_id identify the row rather than describe it —
    moving a membership is a delete plus a create, never a field edit."""
    _mkemp(db_session, "ph-immutable-1", "Pat Immutable")
    resp = await client.put(
        f"/people/ph-immutable-1/projects/{atlas_id}", params={"view_mode": "work"},
        json={"role": "Engineer", "start_date": "2024-01-01",
              "employee_id": "somebody-else"},
        headers=auth_headers("it"),
    )
    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# The read side has to name the row the write side addresses.
# ---------------------------------------------------------------------------

async def test_project_history_exposes_the_id_the_write_path_needs(
    client, db_session, atlas_id
):
    """ProfilePage edits a membership by (person, project), so the id has
    to travel with the row it edits — without it the UI can render project
    history it has no way to address."""
    _mkemp(db_session, "ph-readid-1", "Pat ReadId")
    await client.put(
        f"/people/ph-readid-1/projects/{atlas_id}", params={"view_mode": "work"},
        json={"role": "Engineer", "start_date": "2024-01-01"}, headers=auth_headers("it"),
    )
    resp = await client.get(
        "/people/ph-readid-1", params={"view_mode": "work"}, headers=auth_headers("it"))
    assert resp.status_code == 200, resp.text

    history = resp.json()["project_history"]
    row = next(p for p in history if p["project_id"] == atlas_id)
    assert row["project_name"] == "Project HistoryFixture"
    # Round-trips: the id the read handed back addresses the same row.
    delete = await client.delete(
        f"/people/ph-readid-1/projects/{row['project_id']}", params={"view_mode": "work"},
        headers=auth_headers("it"))
    assert delete.status_code == 204


# ---------------------------------------------------------------------------
# Adding a project by name.
#
# Named rather than picked from a list, because nothing lists projects and
# the person typing knows the name, not the id. get_or_create_project is
# the same path accepting a document's project_entry has always taken.
# ---------------------------------------------------------------------------

async def test_it_adds_a_person_to_a_brand_new_project(client, db_session):
    _mkemp(db_session, "ph-add-1", "Pat Adder")
    resp = await client.post(
        "/people/ph-add-1/projects", params={"view_mode": "work"},
        json={"project_name": "Project Kingfisher", "role": "Platform Lead",
              "start_date": "2025-03-01", "contribution": "Owned the cutover.",
              "project_desc": "Migration of the core platform."},
        headers=auth_headers("it", "it-adder-1"),
    )
    assert resp.status_code == 201, resp.text

    project = db_session.query(Project).filter(Project.name == "Project Kingfisher").one()
    row = _membership(db_session, "ph-add-1", project.id)
    assert row is not None
    assert row.role == "Platform Lead"
    assert row.contribution == "Owned the cutover."
    assert row.start_date == date(2025, 3, 1)
    assert row.end_date is None
    assert project.description == "Migration of the core platform."


async def test_matching_an_existing_name_joins_it_instead_of_forking(
    client, db_session, atlas_id
):
    """Case-insensitive match — otherwise "project atlas" would quietly
    become a second project with the same name."""
    _mkemp(db_session, "ph-add-2", "Pat Joiner")
    before = db_session.query(Project).count()
    resp = await client.post(
        "/people/ph-add-2/projects", params={"view_mode": "work"},
        json={"project_name": "project historyfixture", "role": "Engineer",
              "start_date": "2024-05-01"},
        headers=auth_headers("it"),
    )
    assert resp.status_code == 201, resp.text
    assert db_session.query(Project).count() == before  # nothing new created
    assert _membership(db_session, "ph-add-2", atlas_id) is not None


async def test_adding_someone_already_on_the_project_is_409(client, db_session, atlas_id):
    """"Add" must not silently overwrite a role and dates nobody looked at."""
    _mkemp(db_session, "ph-add-3", "Pat Dupe")
    first = await client.post(
        "/people/ph-add-3/projects", params={"view_mode": "work"},
        json={"project_name": "Project HistoryFixture", "role": "Engineer",
              "start_date": "2024-01-01"}, headers=auth_headers("it"))
    assert first.status_code == 201, first.text

    second = await client.post(
        "/people/ph-add-3/projects", params={"view_mode": "work"},
        json={"project_name": "Project HistoryFixture", "role": "Rewritten",
              "start_date": "2020-01-01"}, headers=auth_headers("it"))
    assert second.status_code == 409, second.text

    row = _membership(db_session, "ph-add-3", atlas_id)
    assert row.role == "Engineer"            # untouched
    assert row.start_date == date(2024, 1, 1)


async def test_omitting_project_desc_leaves_an_existing_one_alone(
    client, db_session, atlas_id
):
    """A description is shared by everyone on the project — adding one more
    person must not blank it."""
    before = db_session.get(Project, atlas_id).description
    assert before  # fixture ships one
    _mkemp(db_session, "ph-add-4", "Pat Preserver")
    resp = await client.post(
        "/people/ph-add-4/projects", params={"view_mode": "work"},
        json={"project_name": "Project HistoryFixture", "role": "Engineer",
              "start_date": "2024-01-01"}, headers=auth_headers("it"))
    assert resp.status_code == 201, resp.text

    db_session.expire_all()
    assert db_session.get(Project, atlas_id).description == before


async def test_add_requires_name_role_and_start(client, db_session):
    _mkemp(db_session, "ph-add-5", "Pat Incomplete")
    resp = await client.post(
        "/people/ph-add-5/projects", params={"view_mode": "work"},
        json={"project_name": "  ", "role": "Engineer", "start_date": "2024-01-01"},
        headers=auth_headers("it"))
    assert resp.status_code == 422, resp.text


async def test_it_cannot_add_a_project_to_their_own_history(client, db_session):
    """The same self-exclusion as edit and remove — otherwise "except their
    own" would have a hole exactly where padding your own record is
    easiest."""
    _mkemp(db_session, "ph-add-self", "Pat SelfAdd")
    resp = await client.post(
        "/people/ph-add-self/projects", params={"view_mode": "work"},
        json={"project_name": "Project Selfmade", "role": "Principal",
              "start_date": "2024-01-01"},
        headers=auth_headers("it", "ph-add-self"))
    assert resp.status_code == 403, resp.text
    assert db_session.query(Project).filter(Project.name == "Project Selfmade").first() is None


@pytest.mark.parametrize("role", [r for r in ALL_ROLES if r != "it"])
async def test_only_it_may_add_project_history(client, db_session, role):
    _mkemp(db_session, f"ph-addrole-{role}", f"Pat {role}")
    resp = await client.post(
        f"/people/ph-addrole-{role}/projects", params={"view_mode": "work"},
        json={"project_name": "Project X", "role": "Engineer",
              "start_date": "2024-01-01"}, headers=auth_headers(role))
    assert resp.status_code == 403, resp.text
