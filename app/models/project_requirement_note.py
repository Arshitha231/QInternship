from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class ProjectRequirementNote(Base):
    """A qualitative requirement recorded against a project — something a
    PRD (or a person) said the engagement needs that isn't a named skill:
    "client is sensitive about timeline slippage", "prefers an
    on-site presence". Sits alongside ProjectSkillRequirement rather than
    inside it, because that table is REPLACE semantics (set_required_skills
    deletes and re-inserts the full set on every write) — a nullable-skill
    row bolted onto it would be silently deleted the next time someone
    confirms a skill list.

    No unique constraint, unlike (project_id, skill_id): a note has no
    natural dedup key, and notes are meant to accumulate across multiple
    PRDs/reviews over a project's life, not be replaced by the latest one.
    """

    __tablename__ = "project_requirement_notes"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False)
    note: Mapped[str] = mapped_column(Text, nullable=False)

    # Which PRD upload this note came from, if any -- a note can also be
    # added by hand with no source document. Nullable so a hand-authored
    # note isn't forced to invent one.
    source_doc_id: Mapped[int | None] = mapped_column(ForeignKey("uploaded_docs.id"), nullable=True)

    # Not a foreign key, same reason as AuditLog.actor_id / UploadedDoc.uploaded_by
    # -- who recorded this has to outlive their employee row.
    created_by: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)

    project = relationship("Project")
    source_doc = relationship("UploadedDoc")
