from sqlalchemy import Enum, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.enums import EmploymentType


class CourseRequirement(Base):
    """Company-side expectation: which courses apply to which people.

    This is the row that lets the profile distinguish "hasn't done X" from
    "X was never expected of them" — without it, an absent status is
    ambiguous and every employee looks non-compliant on every course in the
    catalogue.

    Scope is expressed as a conjunction of optional narrowing clauses; a
    clause left NULL means "don't narrow on this". All-NULL is a legitimate,
    deliberate row: company-wide, everyone.

      org_unit_id       applies to this unit AND everything under it, so a
                        requirement can be filed once at division level
                        instead of once per team. (Employees only ever sit in
                        their most-specific unit — same subtree problem
                        find_people's org_unit filter solves, resolved here by
                        walking *up* from the employee instead of down from
                        the unit; see app/certifications/requirements.py.)
      job_title_keyword case-insensitive substring of job_title. The schema
                        has no "role" or "track" column to key off — title is
                        the only role signal that exists, so "Manager" is how
                        a leadership-track requirement gets expressed.
      employment_type   e.g. compliance training expected of fte only, not
                        contractors.

    Why not a `track` column on Employee: adding one would mean inventing
    track values for ~500 synthetic employees and a second place for them to
    drift out of sync with org_unit/job_title. If HR later owns a real track
    taxonomy, it becomes one more optional clause here, not a rewrite.
    """

    __tablename__ = "course_requirements"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("training_courses.id"), nullable=False)

    org_unit_id: Mapped[int | None] = mapped_column(ForeignKey("org_units.id"), nullable=True)
    job_title_keyword: Mapped[str | None] = mapped_column(String(200), nullable=True)
    employment_type: Mapped[EmploymentType | None] = mapped_column(
        Enum(EmploymentType, native_enum=False, validate_strings=True), nullable=True
    )

    # Free text shown nowhere user-facing yet; exists so the reason a course
    # is expected survives the person who filed the row.
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)

    course = relationship("TrainingCourse")
    org_unit = relationship("OrgUnit")
