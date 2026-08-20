"""app.people.get_people_with_projects -- recent project history for
several people in one call, built entirely from repeated get_person(id)
lookups. Closes the gap a compound request like "5 Terraform people and
their recent projects" fell into: find_people/search_people never return
project data, and get_person only ever looks up one person.
"""
from datetime import date

from app.auth import AuthenticatedUser
from app.people import _MAX_RECENT_PROJECTS, _recent_projects, get_people_with_projects
from app.schemas import ProjectHistoryItem

CALLER = AuthenticatedUser(id="hr-1", role="hr")


def _project(name, *, start, current=False, role="Contributor"):
    return ProjectHistoryItem(
        project_id=hash(name) % 10_000, project_name=name, project_type="project",
        role=role, start_month=start, end_month=None if current else "2025-01", current=current,
    )


# ---------------------------------------------------------------------------
# _recent_projects() -- pure sorting/capping logic, no db involved.
# ---------------------------------------------------------------------------

def test_current_projects_sort_before_past_ones():
    projects = [_project("Past", start="2023-01"), _project("Current", start="2024-06", current=True)]
    ordered = _recent_projects(projects)
    assert ordered[0].project_name == "Current"


def test_within_the_same_current_ness_most_recent_start_month_first():
    projects = [_project("Older", start="2023-01", current=True), _project("Newer", start="2024-06", current=True)]
    ordered = _recent_projects(projects)
    assert [p.project_name for p in ordered] == ["Newer", "Older"]


def test_capped_at_max_recent_projects():
    projects = [_project(f"P{i}", start=f"2020-{i:02d}") for i in range(1, 10)]
    ordered = _recent_projects(projects)
    assert len(ordered) == _MAX_RECENT_PROJECTS


def test_none_project_history_stays_none_not_empty_list():
    # The same absent-not-empty distinction PersonDetail.project_history
    # already carries -- "can't see it" must never look like "has none".
    assert _recent_projects(None) is None


# ---------------------------------------------------------------------------
# get_people_with_projects() -- built on get_person(), never a second,
# differently-filtered read.
# ---------------------------------------------------------------------------

def test_returns_real_project_data_for_a_real_person(db_session):
    people = get_people_with_projects(db_session, CALLER, ["stranger-1"])
    assert len(people) == 1
    assert people[0].full_name == "Sam Stranger"
    assert people[0].recent_projects is not None
    assert any(p.project_name == "Project Atlas" for p in people[0].recent_projects)


def test_an_unresolvable_id_is_dropped_not_an_error(db_session):
    # Deleted, restricted, or simply wrong -- the batch still answers for
    # everyone else rather than failing the whole request over one id,
    # the same redact-never-reject shape get_person's own None already
    # holds to for a single lookup.
    people = get_people_with_projects(db_session, CALLER, ["stranger-1", "no-such-person"])
    assert [p.id for p in people] == ["stranger-1"]


def test_preserves_the_requested_order(db_session):
    people = get_people_with_projects(db_session, CALLER, ["report-1", "stranger-1"])
    assert [p.id for p in people] == ["report-1", "stranger-1"]


def test_an_id_restricted_from_this_caller_is_dropped(db_session):
    # restricted-1 (Rory Restricted) is a fully restricted record --
    # get_person returns None for it to an ordinary caller, and this must
    # inherit that, not surface a person get_person itself would refuse.
    unrelated = AuthenticatedUser(id="stranger-1", role="employee")
    people = get_people_with_projects(db_session, unrelated, ["restricted-1", "report-1"])
    assert [p.id for p in people] == ["report-1"]


def test_a_confidential_project_is_omitted_for_a_non_member_caller(db_session):
    # member-1's only seeded project (Project Secret) is confidential --
    # an unrelated caller must see member-1 (they're not restricted) but
    # not that project, the exact membership-only edge get_person's own
    # project_history building already draws.
    unrelated = AuthenticatedUser(id="stranger-1", role="employee")
    people = get_people_with_projects(db_session, unrelated, ["member-1"])
    assert len(people) == 1
    assert people[0].full_name == "Mel Member"
    project_names = [p.project_name for p in (people[0].recent_projects or [])]
    assert "Project Secret" not in project_names


def test_a_confidential_project_is_visible_to_its_own_member(db_session):
    member_caller = AuthenticatedUser(id="member-1", role="employee")
    people = get_people_with_projects(db_session, member_caller, ["member-1"])
    project_names = [p.project_name for p in (people[0].recent_projects or [])]
    assert "Project Secret" in project_names


def test_writes_one_audit_row_per_person_same_as_individual_lookups(db_session):
    from app.models import AuditLog

    before = db_session.query(AuditLog).filter(AuditLog.actor_id == CALLER.id).count()
    get_people_with_projects(db_session, CALLER, ["report-1", "stranger-1"])
    after = db_session.query(AuditLog).filter(AuditLog.actor_id == CALLER.id).count()
    assert after - before == 2  # one per get_person() call, nothing batched away
