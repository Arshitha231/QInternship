"""Step 6: recursive org chart traversal, replayed over real HTTP requests."""
from tests.conftest import auth_headers


# ---------------------------------------------------------------------------
# Basic traversal, both directions.
# ---------------------------------------------------------------------------

async def test_upward_chain_visible_to_all_roles(client):
    for role in ("employee", "manager", "hr"):
        resp = await client.get("/people/chain-1/org-chart", params={"direction": "up"},
                                headers=auth_headers(role))
        assert resp.status_code == 200, role
        body = resp.json()
        assert [n["id"] for n in body] == ["chain-2", "chain-3"], f"role={role}: {body}"
        assert [n["depth"] for n in body] == [1, 2]


async def test_downward_chain_restricted_to_manager_and_above(client):
    resp = await client.get("/people/chain-3/org-chart", params={"direction": "down"},
                            headers=auth_headers("employee"))
    assert resp.status_code == 200
    assert resp.json() == []  # empty result, not a 403 — same redact-never-reject rule

    for role in ("manager", "hr"):
        resp = await client.get("/people/chain-3/org-chart", params={"direction": "down"},
                                headers=auth_headers(role))
        assert resp.status_code == 200
        body = resp.json()
        assert [n["id"] for n in body] == ["chain-2", "chain-1"], f"role={role}: {body}"
        assert [n["depth"] for n in body] == [1, 2]


async def test_downward_denial_is_200_empty_not_403(client):
    resp = await client.get("/people/chain-3/org-chart", params={"direction": "down"},
                            headers=auth_headers("employee"))
    assert resp.status_code == 200
    assert resp.status_code != 403


# ---------------------------------------------------------------------------
# Record-level restriction applies to every node in the chain, not just root.
# ---------------------------------------------------------------------------

async def test_restricted_node_in_chain_is_omitted_for_non_hr(client):
    resp = await client.get("/people/rchain-1/org-chart", params={"direction": "up"},
                            headers=auth_headers("employee"))
    assert resp.status_code == 200
    ids = [n["id"] for n in resp.json()]
    assert "rchain-2" not in ids  # restricted, filtered out
    assert "rchain-3" in ids      # still reachable past the restricted node


async def test_restricted_node_in_chain_visible_to_hr(client):
    resp = await client.get("/people/rchain-1/org-chart", params={"direction": "up"},
                            headers=auth_headers("hr"))
    assert resp.status_code == 200
    ids = [n["id"] for n in resp.json()]
    assert ids == ["rchain-2", "rchain-3"]


async def test_restricted_root_is_404_not_403(client):
    resp = await client.get("/people/restricted-1/org-chart", params={"direction": "up"},
                            headers=auth_headers("employee"))
    assert resp.status_code == 404
    assert resp.status_code != 403


# ---------------------------------------------------------------------------
# The cycle guard: malformed manager_id data must not hang the query.
# ---------------------------------------------------------------------------

async def test_cyclic_manager_id_does_not_hang(client):
    """A -> manager B -> manager A, with no legitimate top. If the depth cap
    weren't enforced in the recursive term's WHERE clause, this query would
    never terminate. The test completing at all is part of the proof; the
    assertions below additionally pin down that the cap was respected."""
    resp = await client.get("/people/cyclic-a/org-chart", params={"direction": "up", "depth": 10},
                            headers=auth_headers("hr"))
    assert resp.status_code == 200
    body = resp.json()

    # Depth cap of 10 holds even though the chain is infinite.
    assert all(n["depth"] <= 10 for n in body)
    # Deduped: the cycle revisits the same two people repeatedly, but each
    # appears once, at its shallowest depth — not ten alternating entries.
    ids = [n["id"] for n in body]
    assert len(ids) == len(set(ids))
    assert set(ids) == {"cyclic-a", "cyclic-b"}
    assert {n["id"]: n["depth"] for n in body} == {"cyclic-b": 1, "cyclic-a": 2}


async def test_depth_cap_holds_even_if_a_larger_depth_is_requested(client):
    resp = await client.get("/people/cyclic-a/org-chart", params={"direction": "up", "depth": 9999},
                            headers=auth_headers("hr"))
    assert resp.status_code == 200
    assert all(n["depth"] <= 10 for n in resp.json())


# ---------------------------------------------------------------------------
# Every request writes an audit_log row, same as find_people/get_person.
# ---------------------------------------------------------------------------

async def test_org_chart_writes_audit_log(client, db_session):
    from app.models import AuditLog

    before = db_session.query(AuditLog).filter(AuditLog.action == "get_org_chain").count()
    resp = await client.get("/people/chain-1/org-chart", params={"direction": "up"},
                            headers=auth_headers("employee", "org-auditor"))
    assert resp.status_code == 200
    after = db_session.query(AuditLog).filter(AuditLog.action == "get_org_chain").count()
    assert after == before + 1

    row = (
        db_session.query(AuditLog)
        .filter(AuditLog.action == "get_org_chain")
        .order_by(AuditLog.id.desc())
        .first()
    )
    assert row.actor_id == "org-auditor"
    assert row.result_count == 2


async def test_org_chart_audits_even_on_role_denial(client, db_session):
    from app.models import AuditLog

    resp = await client.get("/people/chain-3/org-chart", params={"direction": "down"},
                            headers=auth_headers("employee", "org-auditor-2"))
    assert resp.status_code == 200
    assert resp.json() == []

    row = (
        db_session.query(AuditLog)
        .filter(AuditLog.action == "get_org_chain")
        .order_by(AuditLog.id.desc())
        .first()
    )
    assert row.actor_id == "org-auditor-2"
    assert row.result_count == 0


# ---------------------------------------------------------------------------
# view_mode on the org chart.
#
# get_org_chain had no view_mode parameter at all, so employee mode could not
# be expressed here even though every other read route honours it. An hr
# caller previewing the ordinary view kept the downward direction (44 reports
# against a real dataset, where an ordinary colleague gets none) and kept
# seeing restricted people in the chain.
#
# The asymmetry below is the point: hr/it CHOSE employee mode and lose their
# privileges in it; a manager is pinned to employee mode permanently
# (WORK_MODE_ROLES is hr/it only), so collapsing on the raw mode would delete
# every manager's own team chart rather than close a hole. See
# app.policy.is_previewing_ordinary_view.
# ---------------------------------------------------------------------------

async def test_hr_loses_the_downward_chain_when_previewing_employee_mode(client):
    work = await client.get(
        "/people/chain-3/org-chart",
        params={"direction": "down", "view_mode": "work"}, headers=auth_headers("hr"))
    assert [n["id"] for n in work.json()] == ["chain-2", "chain-1"]

    preview = await client.get(
        "/people/chain-3/org-chart",
        params={"direction": "down", "view_mode": "employee"}, headers=auth_headers("hr"))
    assert preview.status_code == 200
    assert preview.json() == [], "hr kept the downward chain while previewing the ordinary view"

    # ...and that matches what an ordinary colleague actually gets.
    ordinary = await client.get(
        "/people/chain-3/org-chart", params={"direction": "down"}, headers=auth_headers("employee"))
    assert ordinary.json() == preview.json()


async def test_a_manager_keeps_their_own_team_chart(client):
    """A manager never had a work mode to give up. resolve_view_mode pins
    them to employee mode however they ask, so collapsing the direction gate
    on the raw mode would take this away from every manager in the company —
    a capability regression dressed up as a permission fix."""
    for params in ({"direction": "down"},
                   {"direction": "down", "view_mode": "employee"},
                   {"direction": "down", "view_mode": "work"}):
        resp = await client.get("/people/chain-3/org-chart", params=params,
                                headers=auth_headers("manager"))
        assert [n["id"] for n in resp.json()] == ["chain-2", "chain-1"], params


async def test_upward_chain_is_unaffected_by_view_mode(client):
    """Upward is visible to everyone who can see the record at all, so it has
    nothing to lose in employee mode — pinned so a later tightening of the
    downward gate doesn't quietly take the upward one with it."""
    for mode in ("work", "employee"):
        resp = await client.get(
            "/people/chain-1/org-chart", params={"direction": "up", "view_mode": mode},
            headers=auth_headers("hr"))
        assert resp.status_code == 200
        assert len(resp.json()) > 0, mode
