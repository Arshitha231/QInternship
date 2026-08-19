"""Search+Ask merge: the unified /search endpoint. Two properties matter
most and get their own tests rather than being implied by the others —
zero-token for direct mode, and citations never exceeding what the caller
is actually permitted to see.
"""
from app.schemas import ProblemExpert
from app.tool_calling import AssistantTurn, ResolvedToolCall
from app.unified_search import _TOOL_REASONS, _phrase_experts
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

async def test_named_third_party_report_to_query_returns_the_manager_as_the_card(client):
    # The card is the ANSWER (the manager), not the subject of the
    # question. Previously this returned Riley Report's own card while the
    # prose named Morgan Manager -- the UI showed the wrong person.
    resp = await client.get(
        "/search", params={"q": "who does Riley Report report to?"}, headers=auth_headers("hr"),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "assisted"
    trace = body["overview"]["trace"]
    assert trace[0]["tool"] == "get_org_chain"
    assert trace[0]["args"] == {"person": "Riley Report", "direction": "up", "depth": 1}
    assert [r["id"] for r in body["results"]] == ["mgr-1"]
    assert "Morgan Manager" in body["overview"]["answer"]


async def test_named_third_party_possessive_manager_query_returns_the_manager(client):
    resp = await client.get(
        "/search", params={"q": "who is Riley Report's manager?"}, headers=auth_headers("hr"),
    )
    assert resp.status_code == 200
    body = resp.json()
    trace = body["overview"]["trace"]
    assert trace[0]["tool"] == "get_org_chain"
    assert trace[0]["args"] == {"person": "Riley Report", "direction": "up", "depth": 1}
    assert [r["id"] for r in body["results"]] == ["mgr-1"]


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


# ---------------------------------------------------------------------------
# Mode 3 reachability THROUGH the endpoint.
#
# These exist because unit tests didn't catch a real production bug: mode 3
# was verified by calling project_search.find_experts() and
# tool_calling._deterministic_resolve() directly, both of which worked, so
# every test passed. But GET /search gates direct-vs-assisted on
# is_question(), and a described problem is a STATEMENT -- no question mark,
# opens with "our"/"I'm". The feature was unreachable from the search box
# for exactly the phrasing it exists to serve, and only a test that goes in
# through the endpoint can see that.
# ---------------------------------------------------------------------------

async def test_problem_statement_reaches_find_experts_without_a_question_mark(client):
    """The regression. Measured on the deployed app: this text returned five
    loosely-related engineers from direct free-text search, while the same
    text with "?" appended correctly routed to find_experts."""
    resp = await client.get(
        "/search",
        params={"q": "our deploy pipeline keeps failing and I'm stuck on the rollback"},
        headers=auth_headers("hr"),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "assisted", "a described problem must not fall through to direct search"
    assert body["overview"]["trace"][0]["tool"] == "find_experts"


async def test_question_mark_variant_routes_identically(client):
    """The two phrasings must agree. They disagreeing IS the bug."""
    base = "our deploy pipeline keeps failing and I'm stuck on the rollback"
    without = await client.get("/search", params={"q": base}, headers=auth_headers("hr"))
    with_mark = await client.get("/search", params={"q": base + "?"}, headers=auth_headers("hr"))
    assert without.json()["mode"] == with_mark.json()["mode"]
    assert (without.json()["overview"]["trace"][0]["tool"]
            == with_mark.json()["overview"]["trace"][0]["tool"])


async def test_ordinary_free_text_still_stays_direct(client, monkeypatch):
    """The gate widened for problems only. A plain descriptive search must
    still cost zero tokens -- otherwise this fix would have quietly routed
    every free-text query through the assisted path."""
    monkeypatch.setattr(
        "app.unified_search.resolve_intent",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("model must not be called")),
    )
    resp = await client.get(
        "/search", params={"q": "someone good with dashboards and reporting"}, headers=auth_headers("hr"),
    )
    assert resp.status_code == 200
    assert resp.json()["mode"] == "direct"


# ---------------------------------------------------------------------------
# The routing gate (2026-08-18): question SHAPE used to be the whole test, so
# a trailing "?" was the difference between an answer and nothing at all.
# ---------------------------------------------------------------------------

async def test_statement_shaped_relationship_question_reaches_the_router(client):
    """No question mark, no interrogative opener -- but the deterministic
    router can answer it for free, so punctuation must not decide."""
    resp = await client.get(
        "/search", params={"q": "Riley Report's manager"}, headers=auth_headers("hr"),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "assisted"
    assert body["overview"]["trace"][0]["tool"] == "get_org_chain"
    assert [r["id"] for r in body["results"]] == ["mgr-1"]


async def test_an_exact_name_is_a_lookup_not_a_relationship_question(client, monkeypatch):
    """"Riley Report" is a person, not a question about someone called
    Riley -- the surname matches the router's reports-to pattern."""
    monkeypatch.setattr(
        "app.unified_search.resolve_intent",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("model must not be called")),
    )
    resp = await client.get("/search", params={"q": "Riley Report"}, headers=auth_headers("hr"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "direct"
    assert [r["id"] for r in body["results"]] == ["report-1"]


async def test_a_route_naming_nobody_is_not_confident(client, monkeypatch):
    """The router's name group is greedy and keys on the bare word
    "report", so this resolves as a manager question about a person called
    "someone good with dashboards and". A route naming nobody real must not
    count as a confident match."""
    monkeypatch.setattr(
        "app.unified_search.resolve_intent",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("model must not be called")),
    )
    resp = await client.get(
        "/search", params={"q": "someone good with dashboards and reporting"},
        headers=auth_headers("hr"),
    )
    assert resp.status_code == 200
    assert resp.json()["mode"] == "direct"


async def test_coordination_across_values_takes_the_assisted_path(client, monkeypatch):
    """An OR across values is the one shape find_people cannot express --
    its parameters take a single value each. This returned zero results
    without a question mark and seven with one."""
    captured = {}

    def _fake_resolve(message):
        captured["message"] = message
        return AssistantTurn(tool_call=ResolvedToolCall(
            name="search_people",
            arguments={"filters": [{"field": "office", "op": "in", "value": ["Head Office", "Satellite Office"]}]},
        ))

    monkeypatch.setattr("app.unified_search.resolve_intent", _fake_resolve)
    resp = await client.get(
        "/search", params={"q": "anyone in Head Office or Satellite Office"}, headers=auth_headers("hr"),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "assisted"
    assert body["overview"]["trace"][0]["tool"] == "search_people"
    # search_people had no rendering branch at all -- every structured plan
    # came back with zero cards regardless of how many people it matched.
    assert len(body["results"]) > 0
    assert "Found" in body["overview"]["answer"]
    assert body["overview"]["answer"] != "Done."


# ---------------------------------------------------------------------------
# find_experts phrasing. "Who can help?" is the one question whose useful
# answer is not just a name: it is who hit the same thing, and whether they
# can actually be asked right now. Only 3 of 545 seeded employees are away,
# so the branches that matter most are exercised directly here.
# ---------------------------------------------------------------------------

def _expert(name, availability, *, project_id=1, project="Kafka Rebuild",
            role="Lead", excerpt="consumer rebalancing stalled under load",
            retrieval="semantic+keyword"):
    return ProblemExpert(
        id=name, full_name=name, job_title="Engineer", org_unit="Platform",
        availability_status=availability, project_id=project_id, project_name=project,
        role=role, current=True, reason=f"works on {project} as {role}",
        retrieval=retrieval, excerpt=excerpt,
    )


def test_available_expert_is_said_to_be_available():
    answer = _phrase_experts([_expert("Priya Nair", "available")])
    assert "Priya Nair" in answer
    assert "is available" in answer


def test_an_away_top_match_offers_someone_reachable_instead():
    """The old phrasing named the top match and stopped, so it would point
    confidently at someone who is away without ever saying so."""
    answer = _phrase_experts([_expert("Dev Menon", "away"), _expert("Sara Cohen", "available")])
    assert "Dev Menon" in answer and "away" in answer
    # The ranking stays visible: the closest match is still named first,
    # not silently reshuffled behind whoever happens to be free.
    assert answer.index("Dev Menon") < answer.index("Sara Cohen")
    assert "Sara Cohen also worked on it and is available." in answer


def test_nobody_available_says_so_instead_of_implying_otherwise():
    answer = _phrase_experts([_expert("Dev Menon", "away"), _expert("Sara Cohen", "away")])
    assert "the closest match" in answer
    assert "isn't free either" in answer
    # Must not then contradict itself by advertising the very people it
    # just said were unreachable.
    assert "worked on related projects" not in answer


def test_a_single_away_expert_says_there_is_nobody_else():
    answer = _phrase_experts([_expert("Dev Menon", "away")])
    assert "nobody else in our project history has worked on this" in answer


def test_others_count_excludes_everyone_already_named():
    experts = [_expert("Dev Menon", "away"), _expert("Sara Cohen", "available")] + [
        _expert(f"P{i}", "available") for i in range(3)
    ]
    answer = _phrase_experts(experts)
    # 5 experts, 2 named in the sentence -> 3 others, not 4.
    assert "3 others worked on related projects." in answer


def test_a_missing_excerpt_is_reported_as_a_looser_match():
    """The excerpt's absence is meaningful: nothing in the project write-up
    overlapped the problem, so the link is thinner than the ranking says."""
    answer = _phrase_experts([_expert("Dev Menon", "available", excerpt=None)])
    assert "looser match" in answer


def test_keyword_only_retrieval_is_never_phrased_as_a_semantic_match():
    answer = _phrase_experts([_expert("Dev Menon", "available", retrieval="keyword")])
    assert "(keyword match only)" in answer


def test_trace_reasons_are_written_for_people_not_lifted_from_tool_schemas():
    """search_people's schema description is ~400 characters before its
    first period, so deriving the trace line from it rendered a paragraph of
    spec prose about op=in and filter_groups -- a query dump, in the one
    place meant to explain in plain language."""
    for tool, reason in _TOOL_REASONS.items():
        assert len(reason) < 120, f"{tool} reason is too long to read in a trace line"
        assert "op=" not in reason and "filter_groups" not in reason
