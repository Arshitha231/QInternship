from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.enums import SuggestedLinkStatus


class SuggestedOfficialLink(Base):
    """A staged office/role -> candidate mapping, waiting for HR to confirm.

    Mirrors app.models.proposed_change.ProposedChange's staging discipline:
    nothing here is a real community_links edge until an explicit HR action
    (app.community_links.confirm_suggested_official_link) creates one — see
    that module for why confirming fans the edge out to every active
    employee in the office rather than writing a single row.
    """

    __tablename__ = "suggested_official_links"

    __table_args__ = (
        Index("ix_suggested_links_office_role", "office_id", "role_label"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    office_id: Mapped[int] = mapped_column(ForeignKey("offices.id"), nullable=False)
    role_label: Mapped[str] = mapped_column(String(200), nullable=False)
    candidate_employee_id: Mapped[str] = mapped_column(ForeignKey("employees.id"), nullable=False)

    status: Mapped[SuggestedLinkStatus] = mapped_column(
        Enum(SuggestedLinkStatus, native_enum=False, validate_strings=True),
        nullable=False, default=SuggestedLinkStatus.pending,
    )

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)
    reviewed_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    office = relationship("Office", foreign_keys=[office_id])
    candidate = relationship("Employee", foreign_keys=[candidate_employee_id])
