from sqlalchemy import Enum, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.enums import SkillCategory


class Skill(Base):
    __tablename__ = "skills"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    category: Mapped[SkillCategory] = mapped_column(
        Enum(SkillCategory, native_enum=False, validate_strings=True), nullable=False
    )

    # Resolves synonyms in data, e.g. SRE -> Site Reliability Engineering.
    # Null means this row IS the canonical form.
    canonical_id: Mapped[int | None] = mapped_column(ForeignKey("skills.id"), nullable=True)

    canonical = relationship("Skill", remote_side=[id])
