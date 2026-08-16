from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # Deliberately NOT a foreign key to employees — audit records must
    # outlive the employee row they reference.
    actor_id: Mapped[str] = mapped_column(String(36), nullable=False)

    action: Mapped[str] = mapped_column(String(100), nullable=False)
    query_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_count: Mapped[int] = mapped_column(Integer, nullable=False)

    # JSON-encoded list of field names returned, for SQLite/Azure SQL
    # portability (neither has a native array type).
    fields_returned: Mapped[str] = mapped_column(Text, nullable=False)

    # Where the change came from: "ai_extraction" for anything committed out
    # of proposed_changes, otherwise null (a person acting directly).
    #
    # Nullable rather than defaulting to "direct" so existing rows keep
    # meaning exactly what they meant when written — backfilling them with a
    # value nobody recorded would be inventing provenance, which is the one
    # thing an audit table must not do. Read null as "not recorded".
    source: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # Only ever set on the assistant-level row app.tool_calling._write_audit
    # writes (action="assistant") -- "deterministic" / "llm_fixed_tool" /
    # "llm_plan_tool" / "last_resort_fallback" / "direct". Null on every
    # service-level row (find_people/get_person/... each write their own,
    # with no routing concept of their own -- GET /people calls find_people
    # directly, with nothing to record here) and on rows written before
    # this column existed. Same "null means not recorded, never guessed"
    # rule as `source`.
    routed_via: Mapped[str | None] = mapped_column(String(50), nullable=True)

    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)
