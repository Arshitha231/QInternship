from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.enums import NotificationKind


class Notification(Base):
    """A notification that was raised, and to whom.

    There is no email/Slack transport in this project — the row IS the
    delivery for now (app/notifications.py has the single seam where a real
    transport plugs in). Persisting it rather than firing and forgetting is
    what makes the two triggers testable and the demo visible.

    `body` is stored rendered rather than as a template id + params: the
    whole point of the two triggers is that the wording differs by audience
    (an employee is told they didn't *pass*; their management chain is only
    ever told "did not complete"), so what was actually said to each
    recipient is the thing worth keeping.
    """

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    recipient_id: Mapped[str] = mapped_column(ForeignKey("employees.id"), nullable=False, index=True)

    # Who the notification is *about* — the employee whose status changed, or
    # whose birthday/anniversary it is. Equals recipient_id on the employee's
    # own course reminder.
    subject_employee_id: Mapped[str] = mapped_column(ForeignKey("employees.id"), nullable=False)

    # Nullable since the date-driven kinds (birthday, work anniversary) aren't
    # about a course at all. It stays a real FK rather than becoming a loose
    # "subject type + id" pair: two nullable columns that are each required
    # for exactly one kind is easier to reason about than a polymorphic
    # reference the database can't check.
    course_id: Mapped[int | None] = mapped_column(ForeignKey("training_courses.id"), nullable=True)

    kind: Mapped[NotificationKind] = mapped_column(
        Enum(NotificationKind, native_enum=False, validate_strings=True), nullable=False
    )

    # The derived two-value status this notification reports. The underlying
    # four-value status is deliberately NOT stored here: management-facing
    # rows must not carry a pass/fail distinction even in the database, and
    # the employee-facing wording already encodes it in `body`. Empty string
    # for the date-driven kinds, which report no status at all.
    display_status: Mapped[str] = mapped_column(String(20), nullable=False)

    # Identifies the real-world occurrence this notification is about, e.g.
    # "birthday:2026-08-13:<employee id>". The date-driven triggers are a
    # sweep rather than an event: something has to run them daily, and
    # anything that runs daily gets run twice eventually — a retried cron, a
    # restarted container, an operator checking it works. Deduping on this
    # key makes a second sweep a no-op instead of a second birthday message.
    #
    # NULL for the course triggers, which fire from a genuine state change
    # and are already guarded by "an unchanged status notifies nobody".
    event_key: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)

    body: Mapped[str] = mapped_column(Text, nullable=False)

    # Ordering within one status-change event. 0 is always the employee's own
    # notification; the manager chain starts at 1 and increases with distance
    # up the chain. Persisted rather than inferred from created_at, which is
    # only millisecond-resolution and would leave "did the employee hear
    # first?" up to clock ties. See app/notifications.py for why the order
    # matters.
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # How far up the chain this recipient sits (0 = the employee themself,
    # 1 = direct manager, 2 = their manager, ...).
    levels_up: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)

    recipient = relationship("Employee", foreign_keys=[recipient_id])
    subject_employee = relationship("Employee", foreign_keys=[subject_employee_id])
    course = relationship("TrainingCourse")
