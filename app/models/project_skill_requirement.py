from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.enums import SkillLevel


class ProjectSkillRequirement(Base):
    """One skill (and the minimum level needed) that a project's delivery
    actually depends on, as recorded by whoever owns the project or HR --
    app/project_skills.py.

    This is what lets app/continuity.py's delivery-dependency calculation
    tell a real requirement apart from a heuristic: without a row here for
    a given project, that calculation falls back to treating every
    Working/Expert skill an assigned employee happens to hold as a
    candidate dependency, which overcounts (a skill on someone's profile
    that this engagement never actually needed still shows up). Each
    computed DeliveryDependency is tagged "declared" or "inferred"
    accordingly -- see app/schemas.py's DeliveryDependency.source.
    """

    __tablename__ = "project_skill_requirements"
    __table_args__ = (UniqueConstraint("project_id", "skill_id"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), nullable=False)
    skill_id: Mapped[int] = mapped_column(ForeignKey("skills.id"), nullable=False)

    minimum_level: Mapped[SkillLevel] = mapped_column(
        Enum(SkillLevel, native_enum=False, validate_strings=True), nullable=False, default=SkillLevel.working
    )

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)

    project = relationship("Project")
    skill = relationship("Skill")
