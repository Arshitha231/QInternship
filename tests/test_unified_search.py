"""Search+Ask merge: the unified /search endpoint. Two properties matter
most and get their own tests rather than being implied by the others —
zero-token for direct mode, and citations never exceeding what the caller
is actually permitted to see.
"""
from tests.conftest import auth_headers


# ---------------------------------------------------------------------------
# Direct mode: the "zero-token" property is provable, not just assumed —
# resolve_intent (the only thing in this codebase that ever calls the chat
# model) is patched to raise if it's invoked at all. A direct-mode request
# succeeding proves it was never called.
# ---------------------------------------------------------------------------

async def test_direct_mode_never_calls_the_model(client, monkeypatch):
    def _boom(*_args, **_kwargs):
        raise AssertionError("resolve_intent must never be called for a direct-mode query")

    monkeypatch.setattr("app.unified_search.resolve_intent", _boom)

    resp = await client.get(
        "/search", params={"skill": "Site Reliability Engineering"}, headers=auth_headers("employee"),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "direct"
    assert "overview" not in body or body.get("overview") is None
    assert any(p["id"] == "report-1" for p in body["results"])


async def test_direct_mode_bare_attribute_query_never_calls_the_model(client, monkeypatch):
    """Same guarantee for the free-text box, not just structured filters —
    a bare name/skill typed into the single merged input must stay direct."""
    monkeypatch.setattr(
        "app.unified_search.resolve_intent",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("model must not be called")),
    )
    resp = await client.get("/search", params={"q": "Riley Report"}, headers=auth_headers("employee"))
    assert resp.status_code == 200
    assert resp.json()["mode"] == "direct"


async def test_question_shaped_query_is_classified_assisted(client):
    """The classifier itself, independent of what the tool call does —
    interrogative phrasing routes to assisted."""
    resp = await client.get(
        "/search", params={"q": "who could mentor me in Site Reliability Engineering?"},
        headers=auth_headers("employee", "stranger-1"),
    )
    assert resp.status_code == 200
    assert resp.json()["mode"] == "assisted"


# ---------------------------------------------------------------------------
# Assisted mode: citations are a reshaping of an already permission-filtered
# tool result, never an independent lookup — this asserts that invariant
# holds at the HTTP boundary, not just in the service layer.
# ---------------------------------------------------------------------------

async def test_assisted_citations_never_exceed_visible_results(client):
    resp = await client.get(
        "/search", params={"q": "who could mentor me in Site Reliability Engineering"},
        headers=auth_headers("employee", "stranger-1"),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "assisted"

    result_ids = {p["id"] for p in body["results"]}
    citation_ids = {c["id"] for c in body["overview"]["citations"]}
    assert citation_ids <= result_ids, f"citation named someone outside results: {citation_ids - result_ids}"

    # Rory Restricted's record is availability_status=restricted and must
    # never surface, in results or in the overview text, regardless of
    # whether they'd otherwise match the skill.
    assert "restricted-1" not in citation_ids
    assert "restricted-1" not in result_ids
    assert "Rory Restricted" not in body["overview"]["answer"]


async def test_assisted_trace_reflects_the_real_tool_call(client):
    resp = await client.get(
        "/search", params={"q": "who could mentor me in Site Reliability Engineering"},
        headers=auth_headers("employee", "stranger-1"),
    )
    body = resp.json()
    trace = body["overview"]["trace"]
    assert len(trace) == 1
    assert trace[0]["tool"] == "find_mentor"
    assert isinstance(trace[0]["args"], dict)
    assert isinstance(trace[0]["latency_ms"], int)


# ---------------------------------------------------------------------------
# Self-referential relationship/attribute questions ("who is my manager?",
# "who are my direct reports?") must resolve through a typed lookup
# (get_person's person_id="self" / get_org_chain's person="self") on the
# caller's own record — never fall through to find_people's free-text/
# vector search,
# which has no name to match against a first-person question.
#
# "who is my manager?" specifically must surface the MANAGER as the
# headline result, not the caller — get_person(self) technically has the
# right data (manager nested as a field) but makes the caller themself the
# top-level card, which read as "the search highlighted my own name
# instead of my manager's." get_org_chain(self, up, depth=1) puts the
# manager's own record at the top level instead.
# ---------------------------------------------------------------------------

async def test_self_referential_manager_query_uses_get_org_chain_not_get_person(client):
    resp = await client.get("/search", params={"q": "who is my manager?"}, headers=auth_headers("employee", "report-1"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "assisted"
    trace = body["overview"]["trace"]
    assert trace[0]["tool"] == "get_org_chain"
    assert trace[0]["args"] == {"person": "self", "direction": "up", "depth": 1}
    result_ids = {p["id"] for p in body["results"]}
    assert "report-1" not in result_ids  # never highlights the caller
    assert "mgr-1" in result_ids  # highlights the manager instead
    assert "Morgan Manager" in body["overview"]["answer"]


async def test_self_referential_direct_reports_query_uses_get_org_chain_self(client):
    resp = await client.get(
        "/search", params={"q": "who are my direct reports?"}, headers=auth_headers("manager", "mgr-1"),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "assisted"
    trace = body["overview"]["trace"]
    assert trace[0]["tool"] == "get_org_chain"
    assert trace[0]["args"] == {"person": "self", "direction": "down", "depth": 1}
    assert any(p["id"] == "report-1" for p in body["results"])


# ---------------------------------------------------------------------------
# Possessive manager chains ("my manager's manager") must walk that many
# hops up the real reporting chain via get_org_chain, not collapse to the
# same single-hop get_person(self) as plain "my manager" — that collapse
# is what made the buggy version return the caller themself. chain-1 ->
# chain-2 -> chain-3 (Chris Bottom -> Charlie Middle -> Casey Top) is the
# fixture's 3-level chain.
# ---------------------------------------------------------------------------

async def test_self_referential_manager_single_hop_uses_org_chain_depth_one(client):
    """Baseline: plain "my manager" (depth=1) uses the same get_org_chain
    call shape as the multi-hop tests below — one code path for every
    depth, not a special-cased single-hop branch."""
    resp = await client.get("/search", params={"q": "who is my manager?"}, headers=auth_headers("employee", "chain-1"))
    body = resp.json()
    trace = body["overview"]["trace"]
    assert trace[0]["tool"] == "get_org_chain"
    assert trace[0]["args"] == {"person": "self", "direction": "up", "depth": 1}
    result_ids = {p["id"] for p in body["results"]}
    assert "chain-1" not in result_ids
    assert "chain-2" in result_ids


async def test_self_referential_manager_of_manager_walks_two_hops(client):
    resp = await client.get(
        "/search", params={"q": "who is my manager's manager?"}, headers=auth_headers("employee", "chain-1"),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "assisted"
    trace = body["overview"]["trace"]
    assert trace[0]["tool"] == "get_org_chain"
    assert trace[0]["args"] == {"person": "self", "direction": "up", "depth": 2}
    result_ids = {p["id"] for p in body["results"]}
    assert "chain-1" not in result_ids  # never defaults back to self
    assert "chain-3" in result_ids  # Casey Top, two hops above Chris Bottom


async def test_self_referential_manager_chain_three_hops(client):
    """A 3-hop variant of the same phrasing — one more possessive "'s
    manager" asks for depth=3. The fixture chain only has two real
    ancestors above chain-1, so the answer still tops out at chain-3
    (there's nobody a third level up) — the chain walk gracefully returns
    what actually exists rather than erroring or fabricating a person."""
    resp = await client.get(
        "/search", params={"q": "who is my manager's manager's manager?"},
        headers=auth_headers("employee", "chain-1"),
    )
    assert resp.status_code == 200
    body = resp.json()
    trace = body["overview"]["trace"]
    assert trace[0]["tool"] == "get_org_chain"
    assert trace[0]["args"] == {"person": "self", "direction": "up", "depth": 3}
    result_ids = {p["id"] for p in body["results"]}
    assert "chain-1" not in result_ids
    assert "chain-3" in result_ids


# ---------------------------------------------------------------------------
# Named third-party relationship questions ("who does X report to?") must
# extract X's name for a structured find_people(name=...) lookup — a single
# exact match, with X's manager already attached via find_people's own
# single-match enrichment — never forward the whole sentence as `query`
# (free-text/vector search), which is what turned "who does Riley Report
# report to?" into several loosely-related fuzzy name matches instead of
# the one actual person.
# ---------------------------------------------------------------------------

async def test_named_third_party_report_to_query_uses_find_people_by_name(client):
    resp = await client.get(
        "/search", params={"q": "who does Riley Report report to?"}, headers=auth_headers("hr"),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "assisted"
    trace = body["overview"]["trace"]
    assert trace[0]["tool"] == "find_people"
    assert trace[0]["args"] == {"name": "Riley Report"}
    assert len(body["results"]) == 1
    assert body["results"][0]["id"] == "report-1"
    assert body["results"][0]["manager"]["id"] == "mgr-1"
    assert "Morgan Manager" in body["overview"]["answer"]


async def test_named_third_party_possessive_manager_query_uses_find_people_by_name(client):
    resp = await client.get(
        "/search", params={"q": "who is Riley Report's manager?"}, headers=auth_headers("hr"),
    )
    assert resp.status_code == 200
    body = resp.json()
    trace = body["overview"]["trace"]
    assert trace[0]["tool"] == "find_people"
    assert trace[0]["args"] == {"name": "Riley Report"}
    assert len(body["results"]) == 1
    assert body["results"][0]["id"] == "report-1"


# ---------------------------------------------------------------------------
# Skill-miss escalation: a filter-style skill query that misses exactly
# still stays honest and zero-chat-model-cost — it broadens via
# find_people's own semantic search, not a second AI system, and is
# labeled assisted so the UI shows it was broadened.
# ---------------------------------------------------------------------------

async def test_skill_miss_escalates_to_assisted_without_the_model(client, monkeypatch):
    monkeypatch.setattr(
        "app.tool_calling._get_openai_client",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("chat model must not be called for a skill miss")),
    )
    resp = await client.get(
        "/search", params={"skill": "Definitely Not A Real Skill 12345"}, headers=auth_headers("employee"),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "assisted"
    assert body["overview"]["trace"][0]["tool"] == "find_people"


async def test_unique_field_miss_stays_direct_with_no_escalation(client):
    """A name that matches nobody stays a flat empty direct result — no AI
    escalation for unique-identifier fields, only for fuzzy attributes."""
    resp = await client.get(
        "/search", params={"q": "Nobody Named This In The Whole Company"}, headers=auth_headers("employee"),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "direct"
    assert body["results"] == []


# ---------------------------------------------------------------------------
# The dict-return path (no response_model here, same as /ask already does)
# must not silently turn "field was never set" into an explicit `null` —
# that's exactly the boundary-leak response_model_exclude_unset exists to
# prevent on /people, and get_org_chain's PersonSummary cards never set
# manager/direct_reports at all (see app.unified_search._people_and_citations).
# ---------------------------------------------------------------------------

async def test_org_chain_cards_omit_unset_fields_not_null(client, monkeypatch):
    from app.tool_calling import AssistantTurn, ResolvedToolCall

    monkeypatch.setattr(
        "app.unified_search.resolve_intent",
        lambda _msg: AssistantTurn(
            tool_call=ResolvedToolCall(name="get_org_chain", arguments={"person": "Chris Bottom", "direction": "up"})
        ),
    )
    resp = await client.get("/search", params={"q": "who is above chain-1?"}, headers=auth_headers("manager"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "assisted"
    assert len(body["results"]) > 0
    for card in body["results"]:
        assert "direct_reports" not in card, f"direct_reports leaked as an explicit key: {card}"
        assert "manager" not in card, f"manager leaked as an explicit key: {card}"
