"""Search+Ask merge: the unified /search endpoint. Two properties matter
most and get their own tests rather than being implied by the others —
zero-token for direct mode, and citations never exceeding what the caller
is actually permitted to see.
"""
from app.schemas import OfficeOut, PersonSummary, PersonWithProjects, ProblemExpert
from app.tool_calling import AssistantTurn, ResolvedToolCall
from app.unified_search import (
    _TOOL_REASONS, _humanize_args, _people_and_citations, _phrase, _phrase_experts, _phrase_people_matches,
)
from tests.conftest import auth_headers


# ---------------------------------------------------------------------------
# Direct mode: the "zero-token" property is provable, not just assumed —
# resolve_intent (the router; the other chat-model caller, phrase_answer, is
# only ever reached from _build_assisted, which direct mode never calls) is
# patched to raise if it's invoked at all. A direct-mode request succeeding
# proves it was never called.
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


async def test_needs_followup_query_produces_a_multi_step_trace(client, monkeypatch):
    """A question the model's own first call flags needs_followup on (e.g.
    "who on Priya's team knows Terraform and is free next month?" -- no
    single tool call expresses that) must route through execute_chain, not
    execute_with_retry -- and the overview's trace, which until now only
    ever held one entry, must show every step the chain actually ran, not
    just the final one."""
    from app.tool_calling import AssistantTurn, ResolvedToolCall

    monkeypatch.setattr(
        "app.unified_search.resolve_intent",
        lambda *_a, **_k: AssistantTurn(tool_call=ResolvedToolCall(
            name="get_org_chain", arguments={"person": "Priya"}, needs_followup=True)),
    )
    monkeypatch.setattr(
        "app.unified_search.execute_with_retry",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("a chained call must not fall through to the single-call path")),
    )
    monkeypatch.setattr(
        "app.unified_search.execute_chain",
        lambda *_a, **_k: {
            "message": "2 people match.", "tool_call": "find_people", "arguments": {"name": "Y"}, "result": [],
            "steps": [
                {"tool": "get_org_chain", "arguments": {"person": "Priya"}, "latency_ms": 5},
                {"tool": "find_people", "arguments": {"name": "Y"}, "latency_ms": 8},
            ],
        },
    )

    resp = await client.get(
        "/search", params={"q": "who on Priya's team knows Terraform and is free next month?"},
        headers=auth_headers("employee", "stranger-1"),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "assisted"
    trace = body["overview"]["trace"]
    assert [s["tool"] for s in trace] == ["get_org_chain", "find_people"]
    assert all("reason" in s and isinstance(s["latency_ms"], int) for s in trace)


async def test_compound_people_and_projects_query_chains_into_the_new_tool(client, monkeypatch):
    """"5 Terraform people and their recent projects" -- find_people alone
    never returns project data, so this must route through the same
    execute_chain path a needs_followup call already uses, ending on
    get_people_with_projects, not stop after the first step."""
    from app.tool_calling import AssistantTurn, ResolvedToolCall

    monkeypatch.setattr(
        "app.unified_search.resolve_intent",
        lambda *_a, **_k: AssistantTurn(tool_call=ResolvedToolCall(
            name="find_people", arguments={"skill": "Terraform"}, needs_followup=True)),
    )
    monkeypatch.setattr(
        "app.unified_search.execute_chain",
        lambda *_a, **_k: {
            "message": None, "tool_call": "get_people_with_projects",
            "arguments": {"person_ids": ["stranger-1"]},
            "result": [],
            "steps": [
                {"tool": "find_people", "arguments": {"skill": "Terraform"}, "latency_ms": 5},
                {"tool": "get_people_with_projects", "arguments": {"person_ids": ["stranger-1"]}, "latency_ms": 8},
            ],
        },
    )

    resp = await client.get(
        "/search", params={"q": "who has Terraform skills and what are their recent projects?"},
        headers=auth_headers("employee", "stranger-1"),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "assisted"
    trace = body["overview"]["trace"]
    assert [s["tool"] for s in trace] == ["find_people", "get_people_with_projects"]


async def test_a_truncated_chain_still_describes_what_it_found(client, monkeypatch):
    """execute_chain's own truncation note ("Stopped after running out of
    reasoning steps...") describes the budget event, not the caller's
    question -- confirmed live to otherwise completely replace the
    overview's answer, leaving the person who actually matched unnamed
    even though their card still renders below. The note must SUPPLEMENT
    a real answer, never stand in for one."""
    from app.schemas import PersonSummary
    from app.tool_calling import AssistantTurn, ResolvedToolCall

    monkeypatch.setattr(
        "app.unified_search.resolve_intent",
        lambda *_a, **_k: AssistantTurn(tool_call=ResolvedToolCall(
            name="find_people", arguments={"name": "Riley"}, needs_followup=True)),
    )
    person = PersonSummary(
        id="report-1", full_name="Riley Report", job_title="Software Engineer",
        org_unit="Engineering", availability_status="available",
    )
    monkeypatch.setattr(
        "app.unified_search.execute_chain",
        lambda *_a, **_k: {
            "message": "Stopped after running out of reasoning steps — this may be incomplete.",
            "tool_call": "find_people", "arguments": {"name": "Riley"}, "result": [person],
            "steps": [{"tool": "find_people", "arguments": {"name": "Riley"}, "latency_ms": 5}],
            "truncated": "steps",
        },
    )

    resp = await client.get(
        "/search", params={"q": "who on Riley's team knows Terraform and is free next month?"},
        headers=auth_headers("employee", "stranger-1"),
    )
    assert resp.status_code == 200
    answer = resp.json()["overview"]["answer"]
    assert "Riley Report" in answer  # the actual finding, not just the budget note
    assert "Stopped after running out of reasoning steps" in answer  # the note, appended not substituted


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


# ---------------------------------------------------------------------------
# The overview's answer prefers a real model's phrasing of the already-
# filtered result over the deterministic _phrase() template, when one is
# configured and returns something — and still falls back to the template
# otherwise. Every other test in this file exercises the fallback
# implicitly (mock mode, same as production with no CHAT_ENDPOINT/CHAT_KEY
# set); these two make both halves of that fallback explicit.
# ---------------------------------------------------------------------------

async def test_overview_answer_prefers_the_real_models_phrasing(client, monkeypatch):
    monkeypatch.setattr(
        "app.unified_search.phrase_answer",
        lambda question, tool_name, arguments, result: "A model-phrased sentence about Morgan Manager.",
    )
    resp = await client.get("/search", params={"q": "who is my manager?"}, headers=auth_headers("employee", "report-1"))
    assert resp.status_code == 200
    assert resp.json()["overview"]["answer"] == "A model-phrased sentence about Morgan Manager."


async def test_overview_answer_falls_back_to_the_template_when_the_model_has_nothing(client, monkeypatch):
    monkeypatch.setattr("app.unified_search.phrase_answer", lambda *a, **kw: None)
    resp = await client.get("/search", params={"q": "who is my manager?"}, headers=auth_headers("employee", "report-1"))
    assert resp.status_code == 200
    # Same assertion the un-monkeypatched version of this query makes above
    # -- proves the template path alone still answers it correctly.
    assert "Morgan Manager" in resp.json()["overview"]["answer"]


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
# find_people's own semantic search, not a second AI system. mode stays
# "direct" (no overview/trace) because no model call happened; the
# broadening explanation surfaces as a plain `note` instead, so the
# frontend never shows AI framing for something the AI had no part in.
# ---------------------------------------------------------------------------

async def test_skill_miss_broadens_without_the_model_or_ai_framing(client, monkeypatch):
    monkeypatch.setattr(
        "app.tool_calling._get_openai_client",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("chat model must not be called for a skill miss")),
    )
    resp = await client.get(
        "/search", params={"skill": "Definitely Not A Real Skill 12345"}, headers=auth_headers("employee"),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "direct"
    assert body.get("overview") is None
    assert body["note"]


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
        lambda _msg, _db=None: AssistantTurn(
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


async def test_statement_shaped_attribute_query_is_answered_without_the_model(client, monkeypatch):
    """"engineers in Testville" is not question-shaped, describes no
    problem, and matches no deterministic route, so the gate declines to
    spend a model call -- and find_people can only match it against NAMES.
    It used to return nothing. app.text_filters now reads it as the
    structured request it is, still on the direct path and still without
    the model: the escalation this needed was never worth a token."""
    monkeypatch.setattr(
        "app.unified_search.resolve_intent",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("model must not be called")),
    )
    resp = await client.get(
        "/search", params={"q": "engineers in Testville"}, headers=auth_headers("hr"),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "direct"
    assert body["results"], "an attribute query that names real values must not come back empty"
    assert all("Engineer" in p["job_title"] for p in body["results"])


async def test_a_name_that_matches_nobody_is_not_reread_as_filters(client, monkeypatch):
    """The re-read only fires on an already-empty result, so it must not
    turn a genuine "no such person" into some loosely-related list. Nothing
    in this text is a real office, unit, skill or job-title word."""
    monkeypatch.setattr(
        "app.unified_search.resolve_intent",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("model must not be called")),
    )
    resp = await client.get(
        "/search", params={"q": "Nobody Named This In The Whole Company"}, headers=auth_headers("employee"),
    )
    assert resp.status_code == 200
    assert resp.json() == {"mode": "direct", "results": []}


async def test_coordination_across_values_takes_the_assisted_path(client, monkeypatch):
    """An OR across values is the one shape find_people cannot express --
    its parameters take a single value each. This returned zero results
    without a question mark and seven with one."""
    captured = {}

    def _fake_resolve(message, _db=None):
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
    assert "match" in body["overview"]["answer"]
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


# ---------------------------------------------------------------------------
# _phrase_people_matches(): find_people/search_people with more than one
# result. The old phrasing dumped up to 5 bare names plus a count -- a wall
# of text with nothing to distinguish one match from another, and a citation
# list underneath repeating the same names again. This names a handful with
# real context instead, and defers the rest to the card grid or a follow-up.
#
# This is the FALLBACK for when phrase_answer() (app.tool_calling) has no
# real model to ask or its call fails -- mock mode, and any real-mode
# degradation, both still need an answer. It shows the same count (5)
# phrase_answer's own system prompt asks the model for, so a request
# doesn't visibly change shape depending on which of the two wrote it.
# ---------------------------------------------------------------------------

def _person(name, *, job_title="Engineer", city="Seattle", availability="available"):
    return PersonSummary(
        id=name, full_name=name, job_title=job_title, org_unit="Platform",
        office=OfficeOut(id=1, name=f"{city} Office", city=city, country="United States") if city else None,
        availability_status=availability,
    )


def test_shows_only_the_top_few_not_every_match():
    people = [_person(f"Person {i}") for i in range(12)]
    answer = _phrase_people_matches(people)
    assert answer.count("Person") == 5  # 5 named, not all 12 -- same count phrase_answer's prompt asks for
    assert "12 people match" in answer
    assert "7 more match too" in answer


def test_no_remainder_note_when_everyone_is_already_shown():
    people = [_person("A"), _person("B")]
    answer = _phrase_people_matches(people)
    assert "more match" not in answer


def test_each_shown_person_carries_real_context_not_a_bare_name():
    answer = _phrase_people_matches([_person("Priya Sharma", job_title="Data Scientist", city="Austin")])
    assert "Priya Sharma (Data Scientist, Austin · available)" in answer


def test_unavailable_person_is_not_marked_available():
    answer = _phrase_people_matches([_person("Dev Menon", availability="away")])
    assert "· available" not in answer
    assert "Dev Menon (Engineer, Seattle)" in answer


def test_a_person_with_no_office_still_gets_a_readable_descriptor():
    answer = _phrase_people_matches([_person("No Office", city=None)])
    assert "No Office (Engineer · available)" in answer


def test_never_claims_a_best_match_only_a_count():
    # There is no ranking among equally-filter-matching people -- this must
    # never imply one exists.
    answer = _phrase_people_matches([_person(f"P{i}") for i in range(5)])
    assert "best" not in answer.lower()


# ---------------------------------------------------------------------------
# get_people_with_projects wiring: _people_and_citations builds ordinary
# cards from PersonWithProjects (same shape every other tool's cards use --
# recent_projects lives in the phrased answer, not a new card field), and
# _phrase()'s deterministic fallback reuses _phrase_people_matches rather
# than falling through to the generic "Done." every unrecognized tool gets.
# ---------------------------------------------------------------------------

def _person_with_projects(name, *, projects=None):
    return PersonWithProjects(
        id=name, full_name=name, job_title="Engineer", org_unit="Platform",
        availability_status="available", recent_projects=projects,
    )


def test_people_and_citations_builds_ordinary_cards_from_the_new_tool():
    people = [_person_with_projects("Riley Report"), _person_with_projects("Sam Stranger")]
    results, citations = _people_and_citations(None, None, "get_people_with_projects", people)
    assert [r.full_name for r in results] == ["Riley Report", "Sam Stranger"]
    assert [c.full_name for c in citations] == ["Riley Report", "Sam Stranger"]
    assert all(isinstance(r, PersonSummary) for r in results)  # cards are the ordinary shape, not a new one


def test_people_and_citations_handles_an_empty_result():
    results, citations = _people_and_citations(None, None, "get_people_with_projects", [])
    assert results == [] and citations == []


def test_phrase_fallback_for_the_new_tool_names_people_not_just_done():
    people = [_person_with_projects("Riley Report")]
    answer = _phrase("get_people_with_projects", {}, people)
    assert answer != "Done."
    assert "Riley Report" in answer


def test_phrase_fallback_for_the_new_tool_handles_nobody_found():
    answer = _phrase("get_people_with_projects", {}, [])
    assert "Done." != answer
    assert "could be found" in answer or "No" in answer


# ---------------------------------------------------------------------------
# The bounded chain, reachable from /search. It was built, tested and eval'd
# and then only ever callable from POST /ask, which the frontend never calls.
# ---------------------------------------------------------------------------

async def test_a_chained_answer_shows_every_step_it_took(client, monkeypatch):
    """A chained answer that showed only its final call read as though the
    question had been answered by a filter nobody asked for -- the step that
    resolved the team into an actual team is most of the explanation."""
    def _fake_resolve(message, _db=None):
        return AssistantTurn(tool_call=ResolvedToolCall(
            name="get_org_chain",
            arguments={"person": "Morgan Manager", "direction": "down", "depth": 1},
            needs_followup=True,
        ))

    def _fake_next(message, extra_messages=None, history_messages=None):
        return AssistantTurn(tool_call=ResolvedToolCall(
            name="find_people", arguments={"name": "Riley Report"}))

    monkeypatch.setattr("app.unified_search.resolve_intent", _fake_resolve)
    monkeypatch.setattr("app.tool_calling._real_resolve", _fake_next)

    resp = await client.get(
        "/search", params={"q": "who on Morgan Manager's team is available"}, headers=auth_headers("hr"),
    )
    assert resp.status_code == 200
    trace = resp.json()["overview"]["trace"]
    assert [step["tool"] for step in trace] == ["get_org_chain", "find_people"]
    # The intermediate step says what it was FOR, not just what it was.
    assert "filled in the next step" in trace[0]["reason"]


async def test_a_single_call_request_still_takes_exactly_one_call(client, monkeypatch):
    """The chain trigger is the model's own needs_followup on its first
    response, so nothing here re-prompts speculatively after an ordinary
    successful call."""
    calls = []

    def _fake_resolve(message, _db=None):
        return AssistantTurn(tool_call=ResolvedToolCall(
            name="find_people", arguments={"name": "Riley Report"}))

    def _must_not_run(message, extra_messages=None, history_messages=None):
        calls.append(message)
        raise AssertionError("a single-call request must not re-prompt the model")

    monkeypatch.setattr("app.unified_search.resolve_intent", _fake_resolve)
    monkeypatch.setattr("app.tool_calling._real_resolve", _must_not_run)

    resp = await client.get("/search", params={"q": "who is Riley Report?"}, headers=auth_headers("hr"))
    assert resp.status_code == 200
    assert len(resp.json()["overview"]["trace"]) == 1
    assert calls == []


# ---------------------------------------------------------------------------
# The reasoning panel must not read as a query dump. Rewriting the `reason`
# line left the arg chips still rendering raw {field, op, value} JSON.
# ---------------------------------------------------------------------------

def test_search_people_args_are_rendered_as_english():
    args = {"filters": [
        {"field": "office", "op": "in", "value": ["Bangalore", "Singapore"]},
        {"field": "skills", "op": "contains", "value": "Kubernetes"},
    ]}
    out = _humanize_args("search_people", args)
    assert out == {"office": "is one of Bangalore or Singapore", "skills": "includes Kubernetes"}
    # Nothing left that looks like a query.
    assert "filters" not in out and "op" not in str(out) and "field" not in str(out)


def test_two_filters_on_one_field_are_joined_not_clobbered():
    args = {"filters": [
        {"field": "job_title", "op": "contains", "value": "Engineer"},
        {"field": "job_title", "op": "ne", "value": "Engineering Manager"},
    ]}
    assert _humanize_args("search_people", args)["job title"] == (
        "includes Engineer and is not Engineering Manager")


def test_other_tools_arguments_are_left_alone():
    """find_people/get_org_chain arguments are already readable name/value
    pairs -- rewriting them would be churn, not clarity."""
    args = {"person": "Zain Nguyen", "direction": "up", "depth": 1}
    assert _humanize_args("get_org_chain", args) == args
