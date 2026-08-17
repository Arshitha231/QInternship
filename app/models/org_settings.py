from sqlalchemy import ForeignKey, Index, Integer, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

# Fallback for a deployment that has configured neither an office-specific
# nor an org-wide row yet — kept next to the model, not buried in the
# service module, since it's a fact about this table's default shape.
DEFAULT_MENTOR_LINK_DURATION_DAYS = 90


class OrgSettings(Base):
    """HR-configurable org settings — today just the mentor-link duration.

    office_id NULL means the org-wide default; a non-null office_id
    overrides it for that one office. app/community_links.py's duration
    lookup checks the office-specific row first, then the org-wide one,
    then DEFAULT_MENTOR_LINK_DURATION_DAYS if neither exists.

    Only office_id IS NOT NULL rows get a DB-level uniqueness guarantee (the
    filtered index below, same mssql/sqlite/postgresql_where pattern as
    Employee.directory_object_id). Guaranteeing at most one NULL row the
    same way would need a constraint on a constant expression, which isn't
    portable across SQLite and Azure SQL — so the single org-wide row is
    instead kept unique by construction: the service layer always queries
    for the existing NULL row before inserting a new one, the same
    get-or-create discipline app.proposals._get_or_create_project uses for
    Project.
    """

    __tablename__ = "org_settings"

    __table_args__ = (
        Index(
            "ix_org_settings_office_id", "office_id", unique=True,
            mssql_where=text("office_id IS NOT NULL"),
            sqlite_where=text("office_id IS NOT NULL"),
            postgresql_where=text("office_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    office_id: Mapped[int | None] = mapped_column(ForeignKey("offices.id"), nullable=True)
    mentor_link_duration_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_MENTOR_LINK_DURATION_DAYS
    )

    office = relationship("Office", foreign_keys=[office_id])
