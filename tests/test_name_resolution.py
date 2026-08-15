"""Phase 2 of ARCHITECTURE_2.md: the org-chain name resolver (§11/RC2).

Before this, get_org_chain required a UUID the model never actually had for
a named third party ("who is above Shaun Anderson, all the way to the
top?"), so multi-hop chain questions about anyone but the caller had no
working path at all. Covers resolve_person_name() directly, the mock
router's chain-phrasing detection, and the end-to-end dispatch through
execute_tool_call. Fixtures: chain-1/2/3 (Chris Bottom -> Charlie Middle ->
Casey Top, tests/conftest.py) and dup-name-1/2 (two "Dana Ambiguous"s).
"""
from app.auth import AuthenticatedUser
from app.org_chart import resolve_person_name
from app.tool_calling import ResolvedToolCall, _extract_chain_query, execute_tool_call

CALLER = AuthenticatedUser(id="caller-x", role="hr")


# ---------------------------------------------------------------------------
# resolve_person_name()
# ---------------------------------------------------------------------------

def test_exact_match(db_session):
    assert resolve_person_name(db_session, "Casey Top") == "chain-3"


def test_case_insensitive_match(db_session):
    assert resolve_person_name(db_session, "casey top") == "chain-3"


def test_fuzzy_typo_match(db_session):
    # Missing a letter -- close enough to resolve, same tolerance
    # find_people's own fuzzy matching already gives real names.
    assert resolve_person_name(db_session, "Charlie Midle") == "chain-2"


def test_unresolvable_name_returns_none(db_session):
    assert resolve_person_name(db_session, "Zzyzx Nonexistent Qqwrt") is None


def test_empty_name_returns_none(db_session):
    assert resolve_person_name(db_session, "") is None
    assert resolve_person_name(db_session, "   ") is None


def test_ambiguous_duplicate_name_returns_none_not_a_guess(db_session):
    # Two "Dana Ambiguous"s exist -- picking either would silently answer
    # a different question than the one asked.
    assert resolve_person_name(db_session, "Dana Ambiguous") is None


# ---------------------------------------------------------------------------
# _extract_chain_query() -- mock-router phrasing detection
# ---------------------------------------------------------------------------

def test_extract_above_phrasing():
    assert _extract_chain_query("who is above Shaun Anderson, all the way up to the top?") \
        == ("Shaun Anderson", "up")


def test_extract_below_phrasing():
    assert _extract_chain_query("who is below Priya Sharma in the chain") == ("Priya Sharma", "down")


def test_extract_reports_updown_to_phrasing():
    assert _extract_chain_query("show me everyone Katherine Byrne reports up to") \
        == ("Katherine Byrne", "up")


def test_extract_reports_to_name_requires_chain_indicator():
    # Bare "reports to X" alone is ambiguous (could be single-hop) --
    # requires an explicit chain indicator to count here.
    assert _extract_chain_query("who reports to Jordan Reyes") is None
    assert _extract_chain_query("who reports to Jordan Reyes, all the way down the chain") \
        == ("Jordan Reyes", "down")


def test_single_hop_phrasing_does_not_match():
    # These must keep routing through the existing single-hop `report`
    # branch in _mock_resolve, not the new multi-hop one.
    assert _extract_chain_query("who does Sean Wilson report to?") is None
    assert _extract_chain_query("who does Priya Brown report to?") is None
    assert _extract_chain_query("list Jordan Reyes's direct reports") is None


# ---------------------------------------------------------------------------
# End-to-end: execute_tool_call resolves a name and walks the real chain
# ---------------------------------------------------------------------------

def test_dispatch_resolves_name_and_walks_chain_up(db_session):
    tool_call = ResolvedToolCall(
        name="get_org_chain", arguments={"person": "Chris Bottom", "direction": "up", "depth": 2})
    result = execute_tool_call(db_session, CALLER, tool_call)
    assert [n.id for n in result] == ["chain-2", "chain-3"]
    assert [n.depth for n in result] == [1, 2]


def test_dispatch_case_insensitive_and_typo_tolerant(db_session):
    tool_call = ResolvedToolCall(
        name="get_org_chain", arguments={"person": "chris bottom", "direction": "up", "depth": 2})
    result = execute_tool_call(db_session, CALLER, tool_call)
    assert [n.id for n in result] == ["chain-2", "chain-3"]

    tool_call = ResolvedToolCall(
        name="get_org_chain", arguments={"person": "Chris Botom", "direction": "up", "depth": 2})
    result = execute_tool_call(db_session, CALLER, tool_call)
    assert [n.id for n in result] == ["chain-2", "chain-3"]


def test_dispatch_unresolvable_name_returns_none_not_a_crash(db_session):
    tool_call = ResolvedToolCall(
        name="get_org_chain", arguments={"person": "Zzyzx Nonexistent Qqwrt", "direction": "up"})
    assert execute_tool_call(db_session, CALLER, tool_call) is None


def test_dispatch_ambiguous_name_returns_none(db_session):
    tool_call = ResolvedToolCall(name="get_org_chain", arguments={"person": "Dana Ambiguous", "direction": "up"})
    assert execute_tool_call(db_session, CALLER, tool_call) is None


def test_dispatch_self_sentinel_still_works(db_session):
    # Unchanged behavior -- "self" still resolves to the caller, not a name
    # lookup, same never-trust-the-model-for-identity rule as before.
    caller_as_chain1 = AuthenticatedUser(id="chain-1", role="hr")
    tool_call = ResolvedToolCall(
        name="get_org_chain", arguments={"person": "self", "direction": "up", "depth": 2})
    result = execute_tool_call(db_session, caller_as_chain1, tool_call)
    assert [n.id for n in result] == ["chain-2", "chain-3"]


# ---------------------------------------------------------------------------
# ARCHITECTURE_2.md §11/§15 item 7: depth omitted by the model must not
# silently walk the whole chain.
# ---------------------------------------------------------------------------

def test_dispatch_omitted_depth_defaults_to_a_single_hop(db_session):
    # Chris Bottom -> Charlie Middle -> Casey Top is 2 levels up. A model
    # that forgets `depth` on an "up" call used to get the old blanket
    # default of 10, walking the whole chain -- so "who is Chris Bottom's
    # manager" would report Casey Top (depth 2, the top of the chain) as
    # the answer instead of Charlie Middle (depth 1, the actual manager).
    # Omitting depth now must return only the direct hop.
    tool_call = ResolvedToolCall(name="get_org_chain", arguments={"person": "Chris Bottom", "direction": "up"})
    result = execute_tool_call(db_session, CALLER, tool_call)
    assert [n.id for n in result] == ["chain-2"]
    assert [n.depth for n in result] == [1]


def test_dispatch_omitted_depth_defaults_to_a_single_hop_downward_too(db_session):
    tool_call = ResolvedToolCall(name="get_org_chain", arguments={"person": "Casey Top", "direction": "down"})
    result = execute_tool_call(db_session, CALLER, tool_call)
    assert [n.id for n in result] == ["chain-2"]
    assert [n.depth for n in result] == [1]
