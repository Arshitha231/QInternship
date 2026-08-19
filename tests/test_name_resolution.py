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
from app.org_chart import resolve_person, resolve_person_name
from app.schemas import AmbiguousPersonMatch, UnknownPerson
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


def test_dispatch_unresolvable_name_reports_no_such_person(db_session):
    # Was: returned None, indistinguishable from "that person has nobody
    # above them". The caller has to be able to tell those apart -- one is
    # a typo to correct, the other is a real answer.
    tool_call = ResolvedToolCall(
        name="get_org_chain", arguments={"person": "Zzyzx Nonexistent Qqwrt", "direction": "up"})
    result = execute_tool_call(db_session, CALLER, tool_call)
    assert isinstance(result, UnknownPerson)
    assert result.query == "Zzyzx Nonexistent Qqwrt"


def test_dispatch_ambiguous_name_names_the_candidates(db_session):
    # Still never picks one (that was always right) -- but now says which
    # ones it could not choose between, so the caller can answer.
    tool_call = ResolvedToolCall(name="get_org_chain", arguments={"person": "Dana Ambiguous", "direction": "up"})
    result = execute_tool_call(db_session, CALLER, tool_call)
    assert isinstance(result, AmbiguousPersonMatch)
    assert {c.id for c in result.matches} == {"dup-name-1", "dup-name-2"}
    # Job title is what actually separates two people with the same name.
    assert {c.job_title for c in result.matches} == {"Software Engineer", "Product Manager"}


def test_resolver_still_refuses_to_pick_between_duplicates(db_session):
    outcome = resolve_person(db_session, "Dana Ambiguous")
    assert outcome.person_id is None
    assert outcome.is_ambiguous
    assert set(outcome.candidates) == {"dup-name-1", "dup-name-2"}
    # The back-compatible wrapper keeps its old contract for callers that
    # genuinely only need the id.
    assert resolve_person_name(db_session, "Dana Ambiguous") is None


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


# ---------------------------------------------------------------------------
# Nicknames and over-matching (the org-chain bug report, 2026-08-18): the
# resolver matched full_name only and let the fuzzy tier pick a winner out of
# an exact tie, so "Nick" found nobody and a bare surname silently answered
# about whichever person rapidfuzz happened to rank first.
# ---------------------------------------------------------------------------

def test_preferred_name_resolves(db_session):
    assert resolve_person(db_session, "Nick").person_id == "nick-1"


def test_preferred_name_is_case_insensitive_too(db_session):
    assert resolve_person(db_session, "nick").person_id == "nick-1"


def test_full_name_still_wins_for_the_same_person(db_session):
    assert resolve_person(db_session, "Nicholas Rivera").person_id == "nick-1"


def test_a_nickname_two_people_share_is_ambiguous_not_a_coin_flip(db_session):
    outcome = resolve_person(db_session, "Bob")
    assert outcome.is_ambiguous
    assert set(outcome.candidates) == {"nick-2", "nick-3"}


def test_bare_surname_is_ambiguous_rather_than_an_arbitrary_pick(db_session):
    outcome = resolve_person(db_session, "Okonkwo")
    assert outcome.is_ambiguous
    assert set(outcome.candidates) == {"surname-1", "surname-2", "surname-3"}


def test_a_real_typo_still_has_one_clear_winner(db_session):
    # The ambiguity margin must not make ordinary misspellings unresolvable:
    # "Amara Okonkwo" scores far above its two namesakes here.
    assert resolve_person(db_session, "Amara Okonkwa").person_id == "surname-1"


def test_unknown_name_is_unknown_not_ambiguous(db_session):
    outcome = resolve_person(db_session, "Zzyzx Qqwrt")
    assert outcome.is_unknown
    assert not outcome.is_ambiguous


# ---------------------------------------------------------------------------
# Chain phrasings that used to fall through to the model (or extract a name
# with the interrogative still attached).
# ---------------------------------------------------------------------------

def test_possessive_reporting_chain_phrasing():
    # Was: find_people(name="what is Shaun Anderson") -- the leading
    # interrogative was captured as part of the name.
    assert _extract_chain_query("what is Shaun Anderson's reporting chain") \
        == ("Shaun Anderson", "up")


def test_management_chain_for_name_phrasing():
    assert _extract_chain_query("reporting chain for Sean Wilson") == ("Sean Wilson", "up")
    assert _extract_chain_query("show me the management line for Sean Wilson") == ("Sean Wilson", "up")
