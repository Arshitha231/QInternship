from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.enums import SkillLevel, SkillSource


class EmployeeSkill(Base):
    __tablename__ = "employee_skills"
    __table_args__ = (UniqueConstraint("employee_id", "skill_id"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    employee_id: Mapped[str] = mapped_column(ForeignKey("employees.id"), nullable=False)
    skill_id: Mapped[int] = mapped_column(ForeignKey("skills.id"), nullable=False)

    # Capability axis.
    level: Mapped[SkillLevel] = mapped_column(Enum(SkillLevel, native_enum=False, validate_strings=True), nullable=False)
    # Confidence axis — independent of level.
    source: Mapped[SkillSource] = mapped_column(
        Enum(SkillSource, native_enum=False, validate_strings=True), nullable=False
    )
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    employee = relationship("Employee")
    skill = relationship("Skill")
