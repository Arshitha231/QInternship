from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class UploadedDoc(Base):
    """A document somebody uploaded for extraction, plus the text parsed out
    of it.

    The extracted text is stored, not the original bytes. Everything
    downstream — the extraction call, and a reviewer asking "where did this
    come from?" — works from the text, and keeping the source file would
    mean holding an unbounded blob of whatever a status report happens to
    contain, in a database that has no other binary column and no lifecycle
    policy for one. The filename and content type are kept so a reviewer can
    still identify the document they're looking at.
    """

    __tablename__ = "uploaded_docs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    content_type: Mapped[str] = mapped_column(String(150), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)

    extracted_text: Mapped[str] = mapped_column(Text, nullable=False)

    # Same not-a-foreign-key reasoning as AuditLog.actor_id: an uploaded
    # document's provenance has to survive the uploader leaving.
    uploaded_by: Mapped[str] = mapped_column(String(36), nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)

    # Which project a PRD upload is about. NULL for every other use of this
    # table (status reports, resumes aren't single-project-scoped) --
    # set only by app.project_requirements' PRD upload path.
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), nullable=True)

    # Set once, by app.proposals.finalize_document, the moment every
    # actionable proposed_changes row from this document has been decided.
    # At that point extracted_text is overwritten to "" — the row itself
    # (id, filename, uploaded_at) survives, because proposed_changes and
    # doc_subject_matches both hold a non-nullable FK to it and every
    # already-accepted/rejected row is kept, not deleted, as its own audit
    # trail. This column is what distinguishes "empty because nothing
    # parsed" from "emptied on purpose after review finished" — the same
    # confusion app.doc_extraction.correct_call would otherwise be exposed
    # to if it ever ran against a scrubbed row (it checks this first).
    content_scrubbed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    project = relationship("Project")
