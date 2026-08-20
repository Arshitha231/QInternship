import json
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class AssistantTurn(Base):
    """One stored turn of an AssistantConversation -- a PLAN, never a
    result, mirroring app.schemas.HistoryTurn's own fields one-for-one
    (message, tool_call, arguments, assistant_text) and its own discipline:
    tool_calling._history_messages() re-executes tool_call/arguments fresh
    through the enforce()-gated dispatcher on every new turn rather than
    trusting a stored result, so a field revoked between when this row was
    written and when it's replayed is simply absent on replay. Persisting
    a conversation is persisting rows in this exact shape and nothing more
    -- no second, richer representation.

    `arguments` is Text holding JSON, never a native JSON column -- this is
    an explicit, existing convention in this codebase (see
    ProposedChange.proposed_value / EmployeeActionRequest.payload), not a
    style choice: neither SQLite nor Azure SQL has a portable native JSON
    type, and tests/test_sql_portability.py exists to catch exactly this
    kind of violation. to_history_turn()/from_history_turn() are the one
    place that encodes/decodes it -- nothing else in this module or its
    callers should json.loads/json.dumps this column directly.
    """

    __tablename__ = "assistant_turns"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("assistant_conversations.id"), nullable=False, index=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)  # ordering within the conversation

    message: Mapped[str] = mapped_column(Text, nullable=False)
    tool_call: Mapped[str | None] = mapped_column(String(64), nullable=True)
    arguments: Mapped[str | None] = mapped_column(Text, nullable=True)
    assistant_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)

    conversation = relationship("AssistantConversation")

    def to_history_turn(self):
        from app.schemas import HistoryTurn

        return HistoryTurn(
            message=self.message,
            tool_call=self.tool_call,
            arguments=json.loads(self.arguments) if self.arguments is not None else None,
            assistant_text=self.assistant_text,
        )

    @classmethod
    def from_history_turn(cls, conversation_id: int, seq: int, turn) -> "AssistantTurn":
        return cls(
            conversation_id=conversation_id,
            seq=seq,
            message=turn.message,
            tool_call=turn.tool_call,
            arguments=json.dumps(turn.arguments) if turn.arguments is not None else None,
            assistant_text=turn.assistant_text,
        )
