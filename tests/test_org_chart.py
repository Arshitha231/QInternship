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


def test_downward_chain_rbac_rule_is_manager_and_above(db_session):
    """The RBAC rule itself, at the service level, where both modes are
    expressible. Over HTTP only hr/it can actually be in work mode
    (resolve_view_mode pins everyone else), so this is the only place the
    "manager and above" half of the rule can be stated directly."""
    from app.auth import AuthenticatedUser
    from app.org_chart import get_org_chain

    for role in ("manager", "hr"):
        chain = get_org_chain(
            db_session, AuthenticatedUser(id="caller-x", role=role), "chain-3", "down",
            view_mode="work")
        assert [n.id for n in chain] == ["chain-2", "chain-1"], role
    for role in ("employee", "it"):
        chain = get_org_chain(
            db_session, AuthenticatedUser(id="caller-x", role=role), "chain-3", "down",
            view_mode="work")
        assert chain == [], role


async def test_downward_chain_over_http_needs_hr_in_work_mode(client):
    """What the rule reduces to across the wire, which is not the same thing.

    A manager is pinned to employee mode by resolve_view_mode (WORK_MODE_ROLES
    is hr/it only), so they get the same empty downward chain an ordinary
    colleague does. That is the identity guarantee working as specified —
    employee-mode output cannot depend on the caller's role — and it is why
    find_people has never enriched direct_reports for a manager either.

    Giving managers their team back is a product change (adding "manager" to
    WORK_MODE_ROLES), deliberately not smuggled in through the org chart.
    """
    hr = await client.get("/people/chain-3/org-chart",
                          params={"direction": "down", "view_mode": "work"},
                          headers=auth_headers("hr"))
    assert [n["id"] for n in hr.json()] == ["chain-2", "chain-1"]
    assert [n["depth"] for n in hr.json()] == [1, 2]

    for role in ("employee", "manager"):
        resp = await client.get("/people/chain-3/org-chart", params={"direction": "down"},
                                headers=auth_headers(role))
        assert resp.status_code == 200, role
        # Empty result, not a 403 — same redact-never-reject rule.
        assert resp.json() == [], role


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
# Threading view_mode through also made this surface agree with find_people,
# which had always answered from the caller's real mode. They share one
# predicate (app.policy.can_see_direct_reports) but were being handed
# different questions, so for a manager they disagreed outright.
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


async def test_find_people_and_the_org_chart_agree_on_managers(client):
    """The two surfaces are gated by one shared predicate
    (app.policy.can_see_direct_reports) but were handed different questions,
    because the org chart had no view_mode to pass. For a manager they
    disagreed outright: find_people withheld direct_reports, the org chart
    returned the chain. Now both answer from the caller's real mode, so they
    agree for the same person — asserted together, since agreeing is the
    property, not either answer on its own."""
    chart = await client.get("/people/chain-3/org-chart", params={"direction": "down"},
                             headers=auth_headers("manager"))
    listing = await client.get("/people", params={"name": "Casey Top"},
                               headers=auth_headers("manager"))
    assert chart.status_code == 200 and listing.status_code == 200

    chart_has_reports = chart.json() != []
    listing_has_reports = any("direct_reports" in p for p in listing.json())
    assert chart_has_reports == listing_has_reports, (
        f"org chart says {chart_has_reports}, find_people says {listing_has_reports}"
    )


async def test_hr_gets_the_same_agreement_in_both_modes(client):
    """The same property for the role that can actually choose a mode: both
    surfaces grant it in work mode and both withhold it in employee mode."""
    for mode, expected in (("work", True), ("employee", False)):
        chart = await client.get(
            "/people/chain-3/org-chart", params={"direction": "down", "view_mode": mode},
            headers=auth_headers("hr"))
        listing = await client.get(
            "/people", params={"name": "Casey Top", "view_mode": mode}, headers=auth_headers("hr"))
        assert (chart.json() != []) is expected, mode
        assert any("direct_reports" in p for p in listing.json()) is expected, mode


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
