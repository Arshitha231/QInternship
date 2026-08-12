import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Enum, ForeignKey, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.enums import AvailabilityStatus, EmploymentType


class Employee(Base):
    __tablename__ = "employees"

    # A plain `unique=True` column would make this a UNIQUE *constraint*,
    # which SQLite/Postgres treat as "NULL != NULL" (any number of NULL
    # rows allowed) but SQL Server treats as "NULL == NULL" (only one NULL
    # row allowed, full stop) -- broke seeding the very first time this ran
    # against Azure SQL, where most synthetic employees have no linked
    # directory object. A filtered/partial index sidesteps the dialect
    # difference entirely: uniqueness only applies to non-NULL values,
    # identically on every backend.
    __table_args__ = (
        Index(
            "ix_employees_directory_object_id", "directory_object_id", unique=True,
            mssql_where=text("directory_object_id IS NOT NULL"),
            sqlite_where=text("directory_object_id IS NOT NULL"),
            postgresql_where=text("directory_object_id IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))

    # SCIM 2.0 / Microsoft Graph external identity. Nullable: not every
    # synthetic/seed record has one, and there is no live Entra sync yet.
    # Uniqueness enforced by the filtered index in __table_args__ above,
    # not `unique=True` here -- see that comment for why.
    directory_object_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    preferred_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    job_title: Mapped[str] = mapped_column(String(200), nullable=False)

    org_unit_id: Mapped[int] = mapped_column(ForeignKey("org_units.id"), nullable=False)
    office_id: Mapped[int | None] = mapped_column(ForeignKey("offices.id"), nullable=True)
    manager_id: Mapped[str | None] = mapped_column(ForeignKey("employees.id"), nullable=True)

    work_email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    work_phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    slack_handle: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # IANA tz name. Null inherits from office (see effective-timezone rule).
    timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)

    employment_type: Mapped[EmploymentType] = mapped_column(
        Enum(EmploymentType, native_enum=False, validate_strings=True), nullable=False
    )
    hire_date: Mapped[date] = mapped_column(Date, nullable=False)

    # HR-only field; never exposed by the API filter pipeline.
    cost_centre: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # Visible only on own profile or to the direct manager.
    personal_mobile: Mapped[str | None] = mapped_column(String(50), nullable=True)

    availability_status: Mapped[AvailabilityStatus] = mapped_column(
        Enum(AvailabilityStatus, native_enum=False, validate_strings=True),
        nullable=False,
        default=AvailabilityStatus.available,
    )
    # Month/year granularity is enforced at the API layer, not the schema.
    away_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    delegate_id: Mapped[str | None] = mapped_column(ForeignKey("employees.id"), nullable=True)

    bio: Mapped[str | None] = mapped_column(Text, nullable=True)
    photo_url: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Soft delete only. Records are never hard-deleted.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=datetime.now, onupdate=datetime.now
    )

    org_unit = relationship("OrgUnit", foreign_keys=[org_unit_id])
    office = relationship("Office", foreign_keys=[office_id])
    manager = relationship("Employee", remote_side=[id], foreign_keys=[manager_id])
    delegate = relationship("Employee", remote_side=[id], foreign_keys=[delegate_id])
