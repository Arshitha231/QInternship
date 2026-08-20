"""PersonSummary.has_reports -- the per-row "does this person manage anyone"
flag find_people sets on a bulk list.

It exists so the org-tree UI can decide which roster cards get an expand
control without one request per card. The interesting property is the gate:
it carries the same visibility rule as direct_reports and get_org_chain's
downward direction, so employee view mode gets the key ABSENT rather than
false -- advertising an expand that would expand to nothing is worse than
not advertising it.
"""
import pytest
from tests.conftest import auth_headers


async def _names(client, **params):
    resp = await client.get("/people", params=params, headers=auth_headers("hr", "hr-1"))
    assert resp.status_code == 200, resp.text
    return {r["full_name"]: r for r in resp.json()}


@pytest.mark.asyncio
async def test_work_mode_marks_who_manages_people(client, db_session):
    rows = await _names(client, org_unit="Engineering", view_mode="work")
    assert rows, "expected some engineers"
    # Every row carries the flag, not just a single-match enrichment.
    assert all("has_reports" in r for r in rows.values())
    # And it distinguishes: at least one manager and one non-manager.
    flags = {r["has_reports"] for r in rows.values()}
    assert flags == {True, False}, flags


@pytest.mark.asyncio
async def test_employee_mode_withholds_the_flag_entirely(client, db_session):
    resp = await client.get(
        "/people", params={"org_unit": "Engineering", "view_mode": "employee"},
        headers=auth_headers("hr", "hr-1"),
    )
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert rows
    # Absent, not false -- same shape as direct_reports' gate.
    assert all("has_reports" not in r for r in rows)
