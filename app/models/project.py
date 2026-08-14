from sqlalchemy import Boolean, Enum, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.enums import ProjectClassification, ProjectType


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)

    # project | system | function | policy — covers anything the org owns,
    # not just delivery work, so ownership queries work uniformly. NOT a
    # proxy for "client engagement" -- confirmed against the seed data that
    # type=="project" rows are internal/operational work ("Payroll
    # Onboarding Revamp", "HR Operations Vendor Consolidation"), not
    # client-facing. is_client_engagement below is the real signal.
    type: Mapped[ProjectType] = mapped_column(Enum(ProjectType, native_enum=False, validate_strings=True), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    owning_unit_id: Mapped[int] = mapped_column(ForeignKey("org_units.id"), nullable=False)
    owner_id: Mapped[str] = mapped_column(ForeignKey("employees.id"), nullable=False)

    classification: Mapped[ProjectClassification] = mapped_column(
        Enum(ProjectClassification, native_enum=False, validate_strings=True), nullable=False
    )

    # Drives the continuity feature's engagement-intersection scope
    # (app/continuity.py) -- only projects flagged here are ever considered
    # a "client engagement". No separate client_name column: the engagement's
    # own `name` already carries that identity (e.g. "Meridian Health —
    # Claims Platform Modernization"), and /continuity/engagement-exposure's
    # ?client= filter is a substring match against this same field.
    is_client_engagement: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    owning_unit = relationship("OrgUnit")
    owner = relationship("Employee")
