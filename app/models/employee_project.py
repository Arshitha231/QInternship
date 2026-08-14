from datetime import date

from sqlalchemy import Date, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class EmployeeProject(Base):
    __tablename__ = "employee_projects"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    employee_id: Mapped[str] = mapped_column(ForeignKey("employees.id"), nullable=False)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False)
    role: Mapped[str] = mapped_column(String(150), nullable=False)

    # What this person actually did, in prose — as opposed to `role`, which
    # is their title on the project. Separate column rather than reusing
    # `role` because role is String(150) and a title, while this is a
    # sentence or two lifted from a status document; conflating them would
    # both truncate the narrative and corrupt the title.
    #
    # Nullable and empty for every seeded row: it only gets populated by an
    # accepted proposed_change, so most memberships never have one.
    contribution: Mapped[str | None] = mapped_column(Text, nullable=True)

    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    # Null end_date means current. API displays month/year only, never exact dates.
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    employee = relationship("Employee")
    project = relationship("Project")
