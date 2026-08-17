from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.enums import CommunityLinkSource


class CommunityLink(Base):
    """One edge in an employee's private "who to contact for what" graph.

    Two provenances share this table rather than living in separate ones,
    because the read side (GET /community_links) always wants both merged
    into a single per-owner list, and the only places they behave
    differently are edit/delete permission (personal only, see
    app/community_links.py._load_owned_personal) and whether a row can flip
    from official to personal over time (mentor links only) — both are
    single-column checks against `source`/`is_mentor_link`, not a schema
    split.

    Visibility is unconditionally private to owner_employee_id: unlike every
    other role-gated view in this codebase, no role — including hr/it — can
    read another employee's graph. See app/community_links.py's
    list_community_links.
    """

    __tablename__ = "community_links"

    __table_args__ = (
        Index("ix_community_links_owner", "owner_employee_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    owner_employee_id: Mapped[str] = mapped_column(ForeignKey("employees.id"), nullable=False)
    contact_employee_id: Mapped[str] = mapped_column(ForeignKey("employees.id"), nullable=False)

    role_label: Mapped[str] = mapped_column(String(200), nullable=False)

    # Required for personal links (why THIS person) — enforced in
    # app/community_links.py, not here, same reason every other cross-field
    # rule in this codebase lives in the service layer rather than a CHECK
    # constraint: portable across SQLite/Azure SQL, and testable directly.
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    source: Mapped[CommunityLinkSource] = mapped_column(
        Enum(CommunityLinkSource, native_enum=False, validate_strings=True), nullable=False
    )

    # Set for official links tied to a location/team; always null on a
    # personal link.
    office_id: Mapped[int | None] = mapped_column(ForeignKey("offices.id"), nullable=True)
    department_id: Mapped[int | None] = mapped_column(ForeignKey("org_units.id"), nullable=True)

    # Marks an official link as subject to the mentor-expiration rule
    # (created_at + org_settings.mentor_link_duration_days, computed at READ
    # time — see app/community_links.py._resolve_mentor_expiration; never
    # stored as a fixed expiry date, so changing the org setting reshapes
    # future reads instead of silently rewriting rows that already expired
    # under the old value). False for every other official link (payroll,
    # HR contact, ...), which never expire. Never true for a personal link.
    is_mentor_link: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)

    owner = relationship("Employee", foreign_keys=[owner_employee_id])
    contact = relationship("Employee", foreign_keys=[contact_employee_id])
    office = relationship("Office", foreign_keys=[office_id])
    department = relationship("OrgUnit", foreign_keys=[department_id])
