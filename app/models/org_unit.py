from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class OrgUnit(Base):
    __tablename__ = "org_units"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)

    # Self-referencing hierarchy of arbitrary depth. NOT separate
    # department/team tables — see build_order/data-model notes.
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("org_units.id"), nullable=True)
    unit_type: Mapped[str] = mapped_column(String(50), nullable=False)

    parent = relationship("OrgUnit", remote_side=[id])
