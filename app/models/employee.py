import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, Date, DateTime, Enum, ForeignKey, Index, Numeric, String, Text, text
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

    # Self-reported phonetic respelling, e.g. "nuh-VAY-uh" for Navaya --
    # free text, not IPA, so it stays typeable and readable by a colleague
    # who has never seen phonetic notation. Self-service only (see
    # update_own_name_pronunciation in app/people.py, same shape as bio):
    # the record's own subject is the authority on how their name sounds,
    # unlike linkedin_profile which HR edits on anyone's behalf.
    name_pronunciation: Mapped[str | None] = mapped_column(String(200), nullable=True)

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

    # Visible to HR and to the person themselves — and to nobody else, not
    # even their manager. That's a deliberately narrower rule than
    # personal_mobile's (own-profile OR direct manager): a line manager
    # having your mobile number is ordinary, a line manager reading your
    # salary and date of birth off the directory is not.
    #
    # Numeric, not Float: money in binary floating point accumulates rounding
    # error, and while nothing here does arithmetic on it yet, storing it as
    # Float is the kind of thing that's painful to walk back once reports and
    # exports depend on it.
    salary: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)

    # ISO 4217. The dataset spans seven offices across five countries, so a
    # bare number would be actively misleading — 95,000 means very different
    # things in USD and INR.
    salary_currency: Mapped[str | None] = mapped_column(String(3), nullable=True)

    # Full date, not month/day: HR needs it for records, and the person
    # themselves already knows it. Nobody else ever receives it — the
    # birthday notification names the person, never their date of birth or
    # their age.
    date_of_birth: Mapped[date | None] = mapped_column(Date, nullable=True)

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

    # Public professional profile URL. INTERNAL sensitivity (see
    # app/registry.py): a LinkedIn page is already public, so this is no
    # more disclosive than the person's name -- unlike personal_mobile,
    # which is ABAC-gated.
    linkedin_profile: Mapped[str | None] = mapped_column(String(500), nullable=True)

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
