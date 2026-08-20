from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class AssistantConversation(Base):
    """One conversation thread with one of the two assistants (search or
    PRD). Created here in the same migration as ProjectRequirementNote so
    the whole feature ships as one revision (see that migration's
    docstring), but this table and AssistantTurn are not read or written
    by any application code until the conversation-persistence phase --
    they exist unused in between, which is the deliberate price of one
    migration in a repo with this much alembic head-split history.

    Retention is deliberately NOT built here. last_active_at exists so a
    future expiry policy has something to key on, but nothing currently
    prunes old rows -- flagged, not solved, same as this repo's existing
    uploaded_docs.content_scrubbed_at started out.
    """

    __tablename__ = "assistant_conversations"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # Not a foreign key, same reason as AuditLog.actor_id -- a conversation
    # has to remain readable (by its own author, on their next login) even
    # if their employee row is later deactivated.
    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)

    surface: Mapped[str] = mapped_column(String(16), nullable=False)  # "search" | "prd"

    # PRD conversations are project-scoped (one thread per project a user
    # is working on); NULL for a search conversation, which isn't scoped to
    # any one project.
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)
    last_active_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)

    project = relationship("Project")
