"""Privacy/isolation tests for the continuity feature — ARCHITECTURE_2.md
§14's "indirect security" category, and the continuity design doc's §34-35.

Two questions:
  1. Is WorkAuthorizationRecord structurally unreachable through the general
     query pipeline (never merely policy-blocked)?
  2. Does adding or changing a work-authorization record for a real
     employee move so much as one byte of what any ordinary, non-HR-gated
     ranking returns for them?

(2) is, in both source documents' own words, the single most
differentiating test in this suite.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from app.auth import AuthenticatedUser
from app.directory_tools import find_mentor, skill_gap, skill_scarcity
from app.models import WorkAuthorizationRecord
from app.models.enums import VerificationStatus, WorkAuthorizationType
from app.org_chart import get_org_chain
from app.people import find_people
from app.query_plan import Field
from app.registry import REGISTRY
from tests.conftest import auth_headers

HR = AuthenticatedUser(id="isolation-test-hr", role="hr", name="Test HR")
SUBJECT_ID = "report-1"  # tests/conftest.py: Riley Report, holds SRE at expert, reports to mgr-1


# ---------------------------------------------------------------------------
# REGISTRY never gains a continuity-derived entry -- structurally
# unreachable through PeopleQuery/find_people, not merely policy-blocked.
# See app/registry.py's DERIVED_HR comment for the reasoning.
# ---------------------------------------------------------------------------

def test_work_authorization_type_never_enters_the_registry():
    assert "work_authorization_type" not in REGISTRY


def test_continuity_exposure_never_enters_the_registry():
    assert "continuity_exposure" not in REGISTRY


def test_no_continuity_shaped_name_in_the_query_plan_field_literal():
    # Field is a Literal generated from REGISTRY.keys() (app/query_plan.py)
    # -- confirms nothing continuity-shaped snuck into the query-plan's
    # legal field set under any other name either.
    args = [str(a).lower() for a in getattr(Field, "__args__", ())]
    for banned in ("work_authorization", "continuity", "verification_status", "hr_review"):
        assert not any(banned in a for a in args), (
            f"{banned!r}-shaped name found in PeopleQuery's Field literal: {args}"
        )


# ---------------------------------------------------------------------------
# The single most differentiating test: changing a WorkAuthorizationRecord
# must not move a byte of any ordinary ranking's output.
# ---------------------------------------------------------------------------

def _snapshot(db):
    """Every ordinary, non-continuity path SUBJECT_ID can appear in."""
    people = find_people(db, HR, skill="Site Reliability Engineering")
    mentor = find_mentor(db, HR, "Site Reliability Engineering", caller_id="stranger-1")
    gap = skill_gap(db, HR, ["Site Reliability Engineering"])
    scarcity = skill_scarcity(db, HR, "Site Reliability Engineering")
    chain = get_org_chain(db, HR, SUBJECT_ID, "up")
    return (
        [p.model_dump() for p in people],
        [m.model_dump() for m in mentor],
        [g.model_dump() for g in gap],
        [s.model_dump() for s in scarcity],
        [c.model_dump() for c in chain] if chain is not None else None,
    )


def test_work_authorization_record_never_moves_ordinary_rankings(db_session):
    before = _snapshot(db_session)

    # The single most alarming shape a continuity record could take: a
    # verified, current, imminent-review record -- exactly what would drive
    # a "High" engagement exposure if this employee were on a client
    # engagement.
    record = WorkAuthorizationRecord(
        employee_id=SUBJECT_ID, authorization_type=WorkAuthorizationType.h1b,
        effective_from=date.today() - timedelta(days=300),
        effective_until=date.today() + timedelta(days=100),
        next_hr_review_date=date.today() + timedelta(days=5),
        verification_status=VerificationStatus.verified, verified_at=datetime.now(),
        verified_by=None, is_current=True,
    )
    db_session.add(record)
    db_session.commit()

    try:
        after = _snapshot(db_session)
        assert after == before, (
            "Adding a WorkAuthorizationRecord changed an ordinary ranking's output -- "
            "work-authorization data must never influence normal search, mentor "
            "matching, or org-chart results."
        )
    finally:
        db_session.query(WorkAuthorizationRecord).filter_by(employee_id=SUBJECT_ID).delete()
        db_session.commit()


# ---------------------------------------------------------------------------
# HTTP-level: every /continuity/* route 403s for non-hr callers.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("role", ["employee", "manager"])
async def test_continuity_exposure_route_forbidden_for_non_hr(client, role):
    resp = await client.get("/continuity/exposure", headers=auth_headers(role))
    assert resp.status_code == 403


@pytest.mark.parametrize("role", ["employee", "manager"])
async def test_continuity_engagement_exposure_route_forbidden_for_non_hr(client, role):
    resp = await client.get("/continuity/engagement-exposure", headers=auth_headers(role))
    assert resp.status_code == 403


@pytest.mark.parametrize("role", ["employee", "manager"])
async def test_continuity_employee_route_forbidden_for_non_hr(client, role):
    resp = await client.get(f"/continuity/employees/{SUBJECT_ID}", headers=auth_headers(role))
    assert resp.status_code == 403


@pytest.mark.parametrize("role", ["employee", "manager"])
async def test_continuity_review_queue_route_forbidden_for_non_hr(client, role):
    resp = await client.get("/continuity/review-queue", headers=auth_headers(role))
    assert resp.status_code == 403


async def test_continuity_review_queue_route_succeeds_for_hr(client):
    resp = await client.get("/continuity/review-queue", headers=auth_headers("hr"))
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


async def test_continuity_exposure_route_succeeds_for_hr(client):
    resp = await client.get("/continuity/exposure", headers=auth_headers("hr"))
    assert resp.status_code == 200
    assert "by_severity" in resp.json()


async def test_continuity_employee_route_404s_for_unknown_person(client):
    resp = await client.get("/continuity/employees/does-not-exist", headers=auth_headers("hr"))
    assert resp.status_code == 404
