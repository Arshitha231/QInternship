from sqlalchemy import Enum, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.enums import ProjectClassification, ProjectType


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)

    # project | system | function | policy — covers anything the org owns,
    # not just delivery work, so ownership queries work uniformly.
    type: Mapped[ProjectType] = mapped_column(Enum(ProjectType, native_enum=False, validate_strings=True), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    owning_unit_id: Mapped[int] = mapped_column(ForeignKey("org_units.id"), nullable=False)
    owner_id: Mapped[str] = mapped_column(ForeignKey("employees.id"), nullable=False)

    classification: Mapped[ProjectClassification] = mapped_column(
        Enum(ProjectClassification, native_enum=False, validate_strings=True), nullable=False
    )

    owning_unit = relationship("OrgUnit")
    owner = relationship("Employee")
