"""SyntheticCertProvider — the demo's source of truth.

Reads the seeded employee_course_statuses table (see seed.py, which spreads
all four statuses across the existing ~500 synthetic employees). It is a
real implementation of the real interface, not a mock: the profile page and
the notification triggers exercise exactly the code path they will run in
production, and the only thing that changes at go-live is which object the
factory hands back.

Every employee here is a synthetic one — no real Quadrant training data
exists anywhere in this project, same hard constraint as the rest of the
dataset.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.certifications.base import CertStatus, UnknownCourse
from app.models import EmployeeCourseStatus, TrainingCourse
from app.models.enums import CourseStatus


class SyntheticCertProvider:
    """Provider backed by seeded data in our own database."""

    name = "synthetic"

    def __init__(self, db: Session) -> None:
        self._db = db

    # -- interface ---------------------------------------------------------

    def get_status(self, employee_id: str, course_id: str) -> CertStatus:
        course = self._course_by_code(course_id)
        row = self._db.execute(
            select(EmployeeCourseStatus).where(
                EmployeeCourseStatus.employee_id == employee_id,
                EmployeeCourseStatus.course_id == course.id,
            )
        ).scalar_one_or_none()

        if row is None:
            # No record against a course this employee is expected to take
            # is a real, known answer — they have never started it. This is
            # the *only* thing that legitimately produces not_started out of
            # thin air; a provider that cannot answer raises instead.
            return CertStatus(
                employee_id=employee_id, course_id=course.code, course_name=course.name,
                status=CourseStatus.not_started, source=self.name,
            )
        return self._to_dto(row, course)

    def list_statuses(self, employee_id: str) -> list[CertStatus]:
        """Every course this employee has a record against.

        Deliberately *not* "every course they're expected to take" — what's
        expected is our company-side question (app/certifications/
        requirements.py), not something the training system has an opinion
        about. app/certifications/service.py is where the two are joined.
        """
        rows = self._db.execute(
            select(EmployeeCourseStatus, TrainingCourse)
            .join(TrainingCourse, EmployeeCourseStatus.course_id == TrainingCourse.id)
            .where(EmployeeCourseStatus.employee_id == employee_id)
            .order_by(TrainingCourse.name)
        ).all()
        return [self._to_dto(row, course) for row, course in rows]

    # -- internals ---------------------------------------------------------

    def _course_by_code(self, code: str) -> TrainingCourse:
        course = self._db.execute(
            select(TrainingCourse).where(TrainingCourse.code == code)
        ).scalar_one_or_none()
        if course is None:
            raise UnknownCourse(f"No training course with code {code!r}")
        return course

    def _to_dto(self, row: EmployeeCourseStatus, course: TrainingCourse) -> CertStatus:
        return CertStatus(
            employee_id=row.employee_id,
            course_id=course.code,
            course_name=course.name,
            status=row.status,
            attempted_at=row.attempted_at,
            completed_at=row.completed_at,
            source=self.name,
        )
