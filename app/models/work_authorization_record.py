from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Enum, ForeignKey, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.enums import VerificationStatus, WorkAuthorizationType


class WorkAuthorizationRecord(Base):
    """HR-verified work-authorization history for one employee. A row per
    status, not a pair of columns on Employee -- HR's actual pain point is
    tracking *changes* (CPT -> OPT -> STEM OPT -> ...), not one current
    expiry date, so overwriting in place would throw away exactly the
    history HR needs. The continuity service (app/continuity.py) only ever
    reads the row with is_current=True AND verification_status=verified;
    every other row exists purely as an audit trail HR can review.

    Deliberately absent from app/registry.py's REGISTRY -- see that file's
    module docstring. This table is never joined into find_people/PeopleQuery
    at all, so it's structurally unreachable through the general query
    pipeline rather than merely policy-blocked.
    """

    __tablename__ = "work_authorization_records"

    # At most one current record per employee, enforced identically across
    # SQLite/Azure SQL by a filtered index rather than a plain unique
    # constraint -- same reasoning as Employee.directory_object_id's index
    # (app/models/employee.py): the constraint only needs to bind rows where
    # is_current is true, and a partial index says exactly that on every
    # backend, where a bare UNIQUE constraint would not (nothing here is
    # nullable the way directory_object_id is, but the "unique among a
    # subset of rows" shape is the same problem).
    __table_args__ = (
        Index(
            "ix_work_authorization_records_employee_current", "employee_id", unique=True,
            mssql_where=text("is_current = 1"),
            sqlite_where=text("is_current = 1"),
            postgresql_where=text("is_current = true"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    employee_id: Mapped[str] = mapped_column(ForeignKey("employees.id"), nullable=False)

    authorization_type: Mapped[WorkAuthorizationType] = mapped_column(
        Enum(WorkAuthorizationType, native_enum=False, validate_strings=True), nullable=False
    )

    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    # Null: no expiry (citizen / permanent resident).
    effective_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Null: nothing upcoming to review (e.g. citizen/PR records, or a lapsed
    # category HR has closed out).
    next_hr_review_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    verification_status: Mapped[VerificationStatus] = mapped_column(
        Enum(VerificationStatus, native_enum=False, validate_strings=True),
        nullable=False, default=VerificationStatus.pending_verification,
    )
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    verified_by: Mapped[str | None] = mapped_column(ForeignKey("employees.id"), nullable=True)

    # Records that HR has acknowledged the upcoming-review *reminder* for —
    # deliberately not the same thing as having performed the review itself.
    # Doing the actual review means superseding this row with a newly
    # verified one (a new authorization_type/next_hr_review_date), which
    # resets these for free since acknowledgment lives on this row, not a
    # separate table. This pair only silences app/notifications.py's sweep;
    # it never affects GET /continuity/review-queue, which keeps listing the
    # record for as long as it's actually due.
    hr_review_acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    hr_review_acknowledged_by: Mapped[str | None] = mapped_column(ForeignKey("employees.id"), nullable=True)

    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Self-referential: points at the record this one replaces, so HR can
    # walk the history (current -> supersedes -> supersedes -> ...) instead
    # of only ever seeing the latest state.
    supersedes_record_id: Mapped[int | None] = mapped_column(
        ForeignKey("work_authorization_records.id"), nullable=True
    )

    source_document_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    internal_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.now, onupdate=datetime.now
    )

    employee = relationship("Employee", foreign_keys=[employee_id])
    verifier = relationship("Employee", foreign_keys=[verified_by])
    supersedes = relationship("WorkAuthorizationRecord", remote_side=[id])
