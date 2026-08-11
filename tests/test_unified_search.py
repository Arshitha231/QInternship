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
            tool_call=ResolvedToolCall(name="get_org_chain", arguments={"person_id": "chain-1", "direction": "up"})
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
