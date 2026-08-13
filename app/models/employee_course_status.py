from datetime import date, datetime

from sqlalchemy import Date, DateTime, Enum, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.enums import CourseStatus


class EmployeeCourseStatus(Base):
    """One employee's status on one training course.

    NOT the same thing as EmployeeCertification, which is a self-entered
    credential ("I hold a CKA") with an issuer and an expiry. This is
    externally-reported progress against a course in the other team's
    training system.

    Today this table *is* the synthetic dataset SyntheticCertProvider reads
    (see app/certifications/synthetic.py) — the demo's source of truth. Once
    ENABLE_TRAINING_API_SYNC flips on, the training system becomes the source
    of truth and this table's role narrows to a cache of the last status we
    saw, which is what `last_synced_at` and `source` are here to record.
    """

    __tablename__ = "employee_course_statuses"
    __table_args__ = (UniqueConstraint("employee_id", "course_id"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    employee_id: Mapped[str] = mapped_column(ForeignKey("employees.id"), nullable=False)
    course_id: Mapped[int] = mapped_column(ForeignKey("training_courses.id"), nullable=False)

    # Full four-value fidelity, never collapsed to the two-value display
    # derivation — the reminder text depends on not_started vs failed even
    # though both label as "not completed".
    status: Mapped[CourseStatus] = mapped_column(
        Enum(CourseStatus, native_enum=False, validate_strings=True), nullable=False
    )

    # Last attempt of any kind (an in_progress start, a failed sitting, or the
    # passing one); completed_at is set only when status is `completed`. Both
    # nullable: a not_started row has neither.
    attempted_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    completed_at: Mapped[date | None] = mapped_column(Date, nullable=True)

    # Provenance of the row above — "synthetic" for seeded demo data,
    # "training_api" once real sync is on. Deliberately a plain string, not
    # an enum: a third source (a bulk CSV import, say) shouldn't need a
    # migration.
    source: Mapped[str] = mapped_column(String(50), nullable=False, default="synthetic")
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.now, onupdate=datetime.now
    )

    employee = relationship("Employee")
    course = relationship("TrainingCourse")
