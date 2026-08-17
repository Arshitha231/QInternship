"""find_project_owner's name resolution.

Was a single `Project.name.ilike('%name%')` plus `.first()`. Two problems,
both live in production:

  formatting   Every client engagement is named "Client — Project" with an
               em dash. "who owns the Northwind Bank core ledger migration"
               was a total miss against "Northwind Bank — Core Ledger
               Migration" because of one character nobody types.

  ambiguity    `.first()` off an alphabetical ILIKE answered CONFIDENTLY
               when a query matched many. "Migration" matches 16 projects in
               the real directory; the caller got one owner and no signal
               the other 15 existed.

Making matching fuzzier makes the second problem worse, so both are fixed
together: a resolver that returns every plausible match, and a caller that
refuses to pick when there is more than one -- the same discipline as
app.org_chart.resolve_person_name returning None for a duplicated name.
"""
from __future__ import annotations

import pytest

from app.auth import AuthenticatedUser
from app.directory_tools import _name_tokens, _normalise_project_name, find_project_owner, resolve_project_name
from app.models import EmployeeProject, Project
from app.models.enums import ProjectClassification, ProjectType
from app.schemas import AmbiguousProjectMatch, ProjectOwnerResult

HR = AuthenticatedUser(id="hr-1", role="hr", name="HR Caller")


# --- normalisation ---------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("Northwind Bank — Core Ledger Migration", "northwind bank core ledger migration"),
    ("Northwind Bank – Core Ledger Migration", "northwind bank core ledger migration"),  # en dash
    ("Northwind Bank - Core Ledger Migration", "northwind bank core ledger migration"),  # hyphen
    ("  Northwind   Bank  ", "northwind bank"),
])
def test_dash_and_space_variants_normalise_identically(raw, expected):
    assert _normalise_project_name(raw) == expected


def test_filler_words_are_not_matching_signal():
    # "the ... project" must not be what makes two names look alike.
    assert _name_tokens("the Migration project") == {"migration"}
    assert _name_tokens("Payroll Workflow Automation") == {"payroll", "workflow", "automation"}


# --- resolution ------------------------------------------------------------

def _mkproject(db, name, owner_id, unit_id, classification=ProjectClassification.internal):
    p = Project(name=name, type=ProjectType.project, description="test fixture",
                owning_unit_id=unit_id, owner_id=owner_id, classification=classification)
    db.add(p)
    db.flush()
    return p


@pytest.fixture
def named_projects(db_session):
    """Two same-client projects (ambiguous by prefix) plus one distinct."""
    atlas = db_session.query(Project).filter(Project.name == "Project Atlas").one()
    made = [
        _mkproject(db_session, "Testcorp Bank — Core Ledger Migration", atlas.owner_id, atlas.owning_unit_id),
        _mkproject(db_session, "Testcorp Bank — Regulatory Reporting", atlas.owner_id, atlas.owning_unit_id),
        _mkproject(db_session, "Zephyr Airlines — Crew Scheduling", atlas.owner_id, atlas.owning_unit_id),
    ]
    db_session.commit()
    yield made
    for p in made:
        db_session.query(EmployeeProject).filter(EmployeeProject.project_id == p.id).delete()
        db_session.delete(p)
    db_session.commit()


def test_the_em_dash_miss_is_fixed(db_session, named_projects):
    """The exact production failure: the real name has an em dash, the query
    does not, and the old literal ILIKE matched nothing."""
    hits = resolve_project_name(db_session, "Testcorp Bank core ledger migration")
    assert [p.name for p in hits] == ["Testcorp Bank — Core Ledger Migration"]


def test_surrounding_filler_words_do_not_prevent_a_match(db_session, named_projects):
    hits = resolve_project_name(db_session, "the Zephyr Airlines crew scheduling project")
    assert [p.name for p in hits] == ["Zephyr Airlines — Crew Scheduling"]


def test_typo_resolves_via_fuzzy(db_session, named_projects):
    hits = resolve_project_name(db_session, "Zephyr Airlines Crew Schedulng")
    assert [p.name for p in hits] == ["Zephyr Airlines — Crew Scheduling"]


def test_nonsense_resolves_to_nothing(db_session, named_projects):
    assert resolve_project_name(db_session, "zzzz quantum flibbertigibbet") == []


# --- ambiguity -------------------------------------------------------------

def test_ambiguous_query_refuses_to_pick(db_session, named_projects):
    """The behaviour change. Previously this returned one owner, chosen
    alphabetically, with no indication anything else matched."""
    result = find_project_owner(db_session, HR, "Testcorp Bank")
    assert isinstance(result, AmbiguousProjectMatch)
    assert set(result.matches) == {
        "Testcorp Bank — Core Ledger Migration", "Testcorp Bank — Regulatory Reporting"}


def test_unambiguous_query_still_answers(db_session, named_projects):
    result = find_project_owner(db_session, HR, "Zephyr Airlines crew scheduling")
    assert isinstance(result, ProjectOwnerResult)
    assert result.project_name == "Zephyr Airlines — Crew Scheduling"


def test_ambiguous_result_audits_the_real_count(db_session, named_projects):
    """An ambiguous answer logged as result_count=1 would be
    indistinguishable from a resolved one after the fact."""
    from app.models import AuditLog

    find_project_owner(db_session, HR, "Testcorp Bank")
    row = (db_session.query(AuditLog)
           .filter(AuditLog.action == "find_project_owner")
           .order_by(AuditLog.id.desc()).first())
    assert row.result_count == 2


# --- confidential ----------------------------------------------------------

def test_confidential_project_is_not_revealed_by_fuzzier_matching(db_session):
    """Loosening the matcher must not widen what a non-member can reach."""
    result = find_project_owner(db_session, HR, "Project Secret")
    assert result is None
    result = find_project_owner(db_session, HR, "project secrt")  # typo, via fuzzy
    assert result is None


def test_confidential_project_is_not_named_in_an_ambiguity_message(db_session):
    """The filter runs BEFORE the ambiguity check, so a confidential project
    can never appear in a 'did you mean' list -- which would disclose both
    its existence and its name."""
    secret = db_session.query(Project).filter(Project.name == "Project Secret").one()
    atlas = db_session.query(Project).filter(Project.name == "Project Atlas").one()
    sibling = _mkproject(db_session, "Project Secretariat", atlas.owner_id, atlas.owning_unit_id)
    db_session.commit()
    try:
        result = find_project_owner(db_session, HR, "Project Secret")
        names = result.matches if isinstance(result, AmbiguousProjectMatch) else []
        assert secret.name not in names
    finally:
        db_session.query(EmployeeProject).filter(EmployeeProject.project_id == sibling.id).delete()
        db_session.delete(sibling)
        db_session.commit()


def test_a_member_still_reaches_their_confidential_project(db_session):
    member = AuthenticatedUser(id="member-1", role="employee", name="Mel Member")
    result = find_project_owner(db_session, member, "Project Secret")
    assert isinstance(result, ProjectOwnerResult)
    assert result.project_name == "Project Secret"
