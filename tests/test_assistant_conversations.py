"""Tests for app/assistant_conversations.py and the conversation-persistence
wiring in POST /ask, GET /search (assisted mode), and GET /conversations/{surface}.

Uses the real seeded fixture people (report-1 / Riley Report, whose
manager mgr-1 / Morgan Manager is set) so "who is my manager?" routes
through the deterministic router with no model call needed -- same
precedent tests/test_tool_calling.py's own HTTP-adjacent tests use.
"""
from __future__ import annotations

import pytest

from app.assistant_conversations import (
    ConversationNotFound,
    append_turn,
    get_most_recent_conversation,
    load_history,
    open_conversation,
    open_or_continue,
)
from app.auth import AuthenticatedUser
from app.models import AssistantTurn
from app.schemas import HistoryTurn
from tests.conftest import auth_headers

CALLER = AuthenticatedUser(id="report-1", role="employee", name="Riley Report")
OTHER = AuthenticatedUser(id="mgr-1", role="employee", name="Morgan Manager")

MANAGER_QUESTION = "who is my manager?"


# ---------------------------------------------------------------------------
# AssistantTurn.to_history_turn() / from_history_turn() round-trip
# ---------------------------------------------------------------------------

def test_history_turn_round_trips_through_the_model():
    original = HistoryTurn(message="who knows Terraform?", tool_call="find_people", arguments={"skill": "Terraform"})
    row = AssistantTurn.from_history_turn(conversation_id=1, seq=1, turn=original)
    assert row.arguments == '{"skill": "Terraform"}'  # stored as JSON text, not a native JSON column
    assert row.to_history_turn() == original


def test_history_turn_with_no_tool_call_round_trips():
    original = HistoryTurn(message="what's the weather", tool_call=None, arguments=None, assistant_text="I can help with people, teams, skills and projects.")
    row = AssistantTurn.from_history_turn(conversation_id=1, seq=1, turn=original)
    assert row.arguments is None
    assert row.to_history_turn() == original


# ---------------------------------------------------------------------------
# Service-level: open/continue, ownership, load/append
# ---------------------------------------------------------------------------

def test_open_conversation_creates_a_fresh_one(db_session):
    convo = open_conversation(db_session, CALLER, "search")
    assert convo.id is not None
    assert convo.user_id == CALLER.id
    assert convo.surface == "search"


def test_open_or_continue_without_id_opens_a_new_conversation(db_session):
    convo = open_or_continue(db_session, CALLER, "search", None)
    assert convo.user_id == CALLER.id


def test_open_or_continue_with_own_id_continues_it(db_session):
    first = open_conversation(db_session, CALLER, "search")
    continued = open_or_continue(db_session, CALLER, "search", first.id)
    assert continued.id == first.id


def test_open_or_continue_with_someone_elses_id_raises_not_found(db_session):
    theirs = open_conversation(db_session, OTHER, "search")
    with pytest.raises(ConversationNotFound):
        open_or_continue(db_session, CALLER, "search", theirs.id)


def test_open_or_continue_with_wrong_surface_raises_not_found(db_session):
    convo = open_conversation(db_session, CALLER, "search")
    with pytest.raises(ConversationNotFound):
        open_or_continue(db_session, CALLER, "prd", convo.id)


def test_open_or_continue_with_nonexistent_id_raises_not_found(db_session):
    with pytest.raises(ConversationNotFound):
        open_or_continue(db_session, CALLER, "search", 9_999_999)


def test_append_turn_and_load_history_round_trip(db_session):
    convo = open_conversation(db_session, CALLER, "search")
    append_turn(db_session, convo, message="who knows Terraform?",
                tool_call="find_people", arguments={"skill": "Terraform"}, assistant_text=None)
    append_turn(db_session, convo, message="which of those are in Bangalore?",
                tool_call="find_people", arguments={"skill": "Terraform", "office": "Bangalore"}, assistant_text=None)

    history = load_history(db_session, convo)
    assert [h.message for h in history] == ["who knows Terraform?", "which of those are in Bangalore?"]
    assert history[1].arguments == {"skill": "Terraform", "office": "Bangalore"}


def test_get_most_recent_conversation_prefers_the_latest(db_session):
    older = open_conversation(db_session, CALLER, "search")
    append_turn(db_session, older, message="first", tool_call=None, arguments=None, assistant_text="a")
    newer = open_conversation(db_session, CALLER, "search")
    append_turn(db_session, newer, message="second", tool_call=None, arguments=None, assistant_text="b")

    result = get_most_recent_conversation(db_session, CALLER, "search")
    assert result.id == newer.id


def test_get_most_recent_conversation_none_when_caller_has_none(db_session):
    fresh_caller = AuthenticatedUser(id="no-conversations-yet", role="employee")
    assert get_most_recent_conversation(db_session, fresh_caller, "search") is None


def test_prd_conversations_are_scoped_by_project(db_session):
    convo_a = open_conversation(db_session, CALLER, "prd", project_id=101)
    open_conversation(db_session, CALLER, "prd", project_id=202)

    result = get_most_recent_conversation(db_session, CALLER, "prd", project_id=101)
    assert result.id == convo_a.id


def test_prd_project_id_omitted_searches_across_all_projects(db_session):
    # app.assistant_context.recent_facts()'s own use: the caller's most
    # recent PRD conversation regardless of which project it's about.
    # project_id=None must NOT be read as "project_id IS NULL" -- no real
    # PRD conversation (always project-scoped) ever has a null project_id,
    # so that reading would silently find nothing for every real caller.
    open_conversation(db_session, CALLER, "prd", project_id=101)
    newest = open_conversation(db_session, CALLER, "prd", project_id=202)

    result = get_most_recent_conversation(db_session, CALLER, "prd", project_id=None)
    assert result is not None
    assert result.id == newest.id


# ---------------------------------------------------------------------------
# HTTP: POST /ask
# ---------------------------------------------------------------------------

async def test_http_ask_without_conversation_id_returns_a_fresh_one(client):
    resp = await client.post("/ask", json={"message": MANAGER_QUESTION}, headers=auth_headers("employee", "report-1"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["conversation_id"] is not None
    assert body["tool_call"] == "get_org_chain"


async def test_http_ask_continuing_a_conversation_reuses_its_id(client):
    first = await client.post("/ask", json={"message": MANAGER_QUESTION}, headers=auth_headers("employee", "report-1"))
    conversation_id = first.json()["conversation_id"]

    second = await client.post(
        "/ask", json={"message": MANAGER_QUESTION, "conversation_id": conversation_id},
        headers=auth_headers("employee", "report-1"),
    )
    assert second.status_code == 200
    assert second.json()["conversation_id"] == conversation_id


async def test_http_ask_with_someone_elses_conversation_id_404s(client):
    mine = await client.post("/ask", json={"message": MANAGER_QUESTION}, headers=auth_headers("employee", "report-1"))
    conversation_id = mine.json()["conversation_id"]

    resp = await client.post(
        "/ask", json={"message": MANAGER_QUESTION, "conversation_id": conversation_id},
        headers=auth_headers("employee", "mgr-1"),
    )
    assert resp.status_code == 404


async def test_http_ask_with_unknown_conversation_id_404s(client):
    resp = await client.post(
        "/ask", json={"message": MANAGER_QUESTION, "conversation_id": 9_999_999},
        headers=auth_headers("employee", "report-1"),
    )
    assert resp.status_code == 404


async def test_http_ask_threads_context_person_ids_into_extra_context_messages(client, monkeypatch):
    # The "who is the best of these" fix: people currently on the caller's
    # screen ride along as ids, get re-resolved server-side (never trusted
    # from the client), and reach answer_service as an extra context
    # message -- same wiring test_http_prd_ask_contextualizes_the_message_
    # with_the_project_name (tests/test_prd_assistant.py) uses to prove its
    # own fix, applied to this route's own mechanism instead.
    seen = {}

    def fake_answer_service(db, user, message, mode, history, *, extra_context_messages=None):
        seen["message"] = message
        seen["extra_context_messages"] = extra_context_messages
        return {"message": "ok", "tool_call": None, "arguments": None, "result": None}

    monkeypatch.setattr("app.main.answer_service", fake_answer_service)

    resp = await client.post(
        "/ask", json={"message": "who is the best of these?", "context_person_ids": ["report-1", "mgr-1"]},
        headers=auth_headers("employee", "report-1"),
    )
    assert resp.status_code == 200
    # The router's own fast path stays untouched -- the raw question, not a
    # message mutated with the context prefix (that would risk pushing an
    # ordinary search-surface question off the deterministic router).
    assert seen["message"] == "who is the best of these?"

    messages = seen["extra_context_messages"] or []
    assert any(
        "Riley Report [report-1]" in m["content"] and "Morgan Manager [mgr-1]" in m["content"]
        for m in messages
    )


async def test_http_ask_drops_a_context_person_id_the_caller_cannot_see(client, monkeypatch):
    seen = {}

    def fake_answer_service(db, user, message, mode, history, *, extra_context_messages=None):
        seen["extra_context_messages"] = extra_context_messages
        return {"message": "ok", "tool_call": None, "arguments": None, "result": None}

    monkeypatch.setattr("app.main.answer_service", fake_answer_service)

    # restricted-1 (Rory Restricted) is only visible to HR -- an employee
    # caller sending its id as context must not get it echoed back.
    resp = await client.post(
        "/ask", json={"message": "who is the best of these?", "context_person_ids": ["report-1", "restricted-1"]},
        headers=auth_headers("employee", "report-1"),
    )
    assert resp.status_code == 200
    content = "".join(m["content"] for m in (seen["extra_context_messages"] or []))
    assert "Riley Report" in content
    assert "Rory Restricted" not in content


# ---------------------------------------------------------------------------
# HTTP: GET /search (assisted mode gets a conversation_id, direct mode doesn't)
# ---------------------------------------------------------------------------

async def test_http_assisted_search_gets_a_conversation_id(client):
    resp = await client.get("/search", params={"q": MANAGER_QUESTION}, headers=auth_headers("employee", "report-1"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "assisted"
    assert body["conversation_id"] is not None


async def test_http_direct_search_has_no_conversation_id(client):
    resp = await client.get("/search", params={"q": "Riley Report"}, headers=auth_headers("employee"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "direct"
    assert "conversation_id" not in body


# ---------------------------------------------------------------------------
# HTTP: GET /conversations/{surface}
# ---------------------------------------------------------------------------

async def test_http_get_conversation_empty_when_none_exists(client):
    resp = await client.get("/conversations/search", headers=auth_headers("employee", "no-search-history-yet"))
    assert resp.status_code == 200
    assert resp.json() == {"conversation_id": None, "turns": []}


async def test_http_get_conversation_rehydrates_after_asking(client):
    ask = await client.post("/ask", json={"message": MANAGER_QUESTION}, headers=auth_headers("employee", "report-1"))
    conversation_id = ask.json()["conversation_id"]

    resp = await client.get("/conversations/search", headers=auth_headers("employee", "report-1"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["conversation_id"] == conversation_id
    assert body["turns"][-1]["message"] == MANAGER_QUESTION
    assert body["turns"][-1]["tool_call"] == "get_org_chain"


async def test_http_get_conversation_unknown_surface_404s(client):
    resp = await client.get("/conversations/not-a-real-surface", headers=auth_headers("employee"))
    assert resp.status_code == 404


async def test_http_get_prd_conversation_forbidden_for_non_hr(client):
    resp = await client.get("/conversations/prd", headers=auth_headers("employee"))
    assert resp.status_code == 403


async def test_http_get_prd_conversation_allowed_for_hr(client):
    resp = await client.get("/conversations/prd", params={"project_id": 1}, headers=auth_headers("hr"))
    assert resp.status_code == 200
    assert resp.json() == {"conversation_id": None, "turns": []}
