from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.enums import EmployeeActionStatus, EmployeeActionType


class EmployeeActionRequest(Base):
    """Maker-checker for restrict/deactivate/create: app.writes' three
    request_* functions stage one of these instead of applying the change
    directly. Only app.writes.approve_action_request, called by whoever this
    row's own approver_id names, actually flips availability_status or
    is_active or inserts an employee — see that function for why the
    approver is resolved and stamped once, at request time, rather than
    re-derived when the approval comes in.

    requested_by/approver_id/resolved_by are String(36), not real foreign
    keys, same reasoning as AuditLog.actor_id: a request's provenance has to
    survive the people involved leaving. target_employee_id IS a real FK,
    for the two action types that have a target at all.
    """

    __tablename__ = "employee_action_requests"

    __table_args__ = (
        Index("ix_employee_action_requests_approver_status", "approver_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    action_type: Mapped[EmployeeActionType] = mapped_column(
        Enum(EmployeeActionType, native_enum=False, validate_strings=True), nullable=False
    )
    # Null for exactly one action type: `create`, where the employee this
    # request is about does not exist yet and the FK therefore has nothing
    # to point at. Every other action type sets it, and
    # app.writes.request_* is what guarantees that pairing — the column
    # can't express "required for these two types only" on its own.
    target_employee_id: Mapped[str | None] = mapped_column(ForeignKey("employees.id"), nullable=True)

    # The proposed employee's fields, as JSON, for `create` requests only —
    # null for restrict/deactivate, which name an existing row instead.
    #
    # Deliberately a frozen snapshot of what was actually asked for, not a
    # half-built Employee row with is_active=False: a pending request is not
    # a person yet, and a real row would be reachable by every query that
    # forgets to filter it out (search indexing, the org chart, the
    # deactivated-employees list, uniqueness checks). Keeping it inert text
    # until approval means "proposed" and "employed" cannot be confused by
    # any code path that doesn't deliberately open this column.
    payload: Mapped[str | None] = mapped_column(Text, nullable=True)

    requested_by: Mapped[str] = mapped_column(String(36), nullable=False)
    # Resolved once, at request time, by app.writes._resolve_approver --
    # never re-derived when the approval comes in, so the org chart moving
    # between request and approval can't silently redirect who was actually
    # authorized to decide. Nullable only because "no reachable approver
    # exists" is itself a real, reportable outcome (see _resolve_approver),
    # not an error the row's own shape should make unrepresentable.
    approver_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    status: Mapped[EmployeeActionStatus] = mapped_column(
        Enum(EmployeeActionStatus, native_enum=False, validate_strings=True),
        nullable=False, default=EmployeeActionStatus.pending,
    )

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    target_employee = relationship("Employee")
