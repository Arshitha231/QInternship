from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.enums import ResolutionStatus


class DocSubjectMatch(Base):
    """One row per person MENTIONED in a document — the disambiguation step
    itself, kept separate from the field-level proposed_changes rows it
    gates.

    Separate table, not a status column on proposed_changes, because one
    unresolved person blocks many field-level proposals at once (a project
    doc mentioning "John" with three contribution lines and two skills
    still names John exactly once) — the ambiguity is a property of the
    PERSON mentioned, not of any individual field claim about them. This is
    also why extraction never auto-assigns an employee_id, even on a single
    confident match: the correct number of candidates for "John" among
    three Johns is three, always, and collapsing that to one silently here
    would be exactly the guess the whole table exists to prevent.

    candidate_employee_ids is stored as a ranked list — the model's
    confidence in a match is a scoring input, never a threshold that skips
    review. See app.doc_extraction.rank_candidates for how the list is
    built (email match ranks highest, name-only lowest, department/team
    mention a weak tiebreaker).
    """

    __tablename__ = "doc_subject_matches"

    __table_args__ = (
        Index("ix_doc_subject_matches_doc_status", "source_doc_id", "resolution_status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    source_doc_id: Mapped[int] = mapped_column(ForeignKey("uploaded_docs.id"), nullable=False)

    extracted_name: Mapped[str] = mapped_column(String(200), nullable=False)

    # JSON-encoded {"email": ..., "phone": ..., "department": ...} — only
    # the keys the document actually evidenced are present, matching the
    # rest of this app's absent-means-not-found convention rather than a
    # dict of nulls.
    extracted_signals: Mapped[str] = mapped_column(Text, nullable=False, default="{}")

    # JSON-encoded ranked list of employee ids, best match first. Empty list
    # is a real, valid state — it's what makes new_hire_candidate provable
    # rather than assumed: nobody plausible was found, not "nobody looked."
    candidate_employee_ids: Mapped[str] = mapped_column(Text, nullable=False, default="[]")

    resolution_status: Mapped[ResolutionStatus] = mapped_column(
        Enum(ResolutionStatus, native_enum=False, validate_strings=True),
        nullable=False, default=ResolutionStatus.unresolved,
    )

    # Set only when resolution_status == resolved. A new_hire_candidate row
    # stays employee_id-less forever by design — see ResolutionStatus's own
    # docstring for why that's a distinct terminal state rather than a null
    # resolved_employee_id under resolved.
    resolved_employee_id: Mapped[str | None] = mapped_column(
        ForeignKey("employees.id"), nullable=True
    )

    # Not a foreign key, same reason as AuditLog.actor_id — who resolved
    # this has to outlive their employee row.
    resolved_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    source_doc = relationship("UploadedDoc")
    resolved_employee = relationship("Employee")
    proposed_changes = relationship("ProposedChange", back_populates="subject_match")
