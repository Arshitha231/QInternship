"""Server-side persistence for the two chat assistants (search and PRD).

Store the plans, not the answers: app.schemas.HistoryTurn already carries
exactly the right shape and discipline for one turn (message, tool_call,
arguments, assistant_text), and app.tool_calling._history_messages()
re-executes tool_call/arguments fresh through the enforce()-gated
dispatcher on every new turn rather than trusting a stored result -- so a
field revoked between when a turn was written and when it's replayed is
simply absent on replay. Persisting a conversation is persisting
AssistantTurn rows in that exact shape and nothing more; this module is
the only place that reads/writes AssistantConversation/AssistantTurn.

Ownership, not role: a caller may only ever see their OWN conversations.
A conversation_id that exists but belongs to someone else is a 404, same
as one that doesn't exist at all -- never a 403, which would confirm the
id refers to something real.
"""
from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.auth import AuthenticatedUser
from app.models import AssistantConversation, AssistantTurn
from app.schemas import HistoryTurn

Surface = str  # "search" | "prd"


class ConversationNotFound(Exception):
    """conversation_id doesn't exist, or exists but belongs to a
    different caller, or belongs to the wrong surface -- all three
    collapse to this one exception/404, never a 403."""


def get_owned_conversation(
    db: Session, caller: AuthenticatedUser, conversation_id: int, surface: Surface,
) -> AssistantConversation:
    convo = db.get(AssistantConversation, conversation_id)
    if convo is None or convo.user_id != caller.id or convo.surface != surface:
        raise ConversationNotFound(f"No {surface} conversation {conversation_id} for this caller")
    return convo


def open_conversation(
    db: Session, caller: AuthenticatedUser, surface: Surface, project_id: int | None = None,
) -> AssistantConversation:
    convo = AssistantConversation(user_id=caller.id, surface=surface, project_id=project_id)
    db.add(convo)
    db.commit()
    db.refresh(convo)
    return convo


def open_or_continue(
    db: Session, caller: AuthenticatedUser, surface: Surface, conversation_id: int | None,
    project_id: int | None = None,
) -> AssistantConversation:
    """The route-level entry point: conversation_id given -> must already
    be this caller's own conversation on this surface (ConversationNotFound
    otherwise); absent -> a fresh conversation is opened. Every /ask or
    /prd/ask call resolves to a real conversation either way -- there is
    no "unpersisted" turn, only a choice about whether THIS turn's context
    comes from the store (conversation_id given) or the caller's own
    client-supplied history (conversation_id absent, the pre-persistence
    path, kept working exactly as before so the frontend can migrate on
    its own schedule)."""
    if conversation_id is not None:
        return get_owned_conversation(db, caller, conversation_id, surface)
    return open_conversation(db, caller, surface, project_id)


def load_history(db: Session, conversation: AssistantConversation) -> list[HistoryTurn]:
    rows = (
        db.query(AssistantTurn)
        .filter(AssistantTurn.conversation_id == conversation.id)
        .order_by(AssistantTurn.seq)
        .all()
    )
    return [row.to_history_turn() for row in rows]


def append_turn(
    db: Session, conversation: AssistantConversation, message: str,
    tool_call: str | None, arguments: dict | None, assistant_text: str | None,
) -> None:
    from datetime import datetime

    next_seq = (
        db.query(func.max(AssistantTurn.seq)).filter(AssistantTurn.conversation_id == conversation.id).scalar() or 0
    ) + 1
    turn = HistoryTurn(message=message, tool_call=tool_call, arguments=arguments, assistant_text=assistant_text)
    db.add(AssistantTurn.from_history_turn(conversation.id, next_seq, turn))
    conversation.last_active_at = datetime.now()
    db.commit()


def get_most_recent_conversation(
    db: Session, caller: AuthenticatedUser, surface: Surface, project_id: int | None = None,
) -> AssistantConversation | None:
    """For GET /conversations/{surface} rehydration on page load. PRD
    conversations are project-scoped -- passing project_id narrows to that
    project specifically, so HR working on project B never rehydrates
    project A's thread; omitted (None) for the search surface, which
    isn't scoped to any one project."""
    query = db.query(AssistantConversation).filter(
        AssistantConversation.user_id == caller.id, AssistantConversation.surface == surface)
    if surface == "prd":
        query = query.filter(AssistantConversation.project_id == project_id)
    return query.order_by(AssistantConversation.last_active_at.desc()).first()
