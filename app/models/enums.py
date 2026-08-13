import enum


class EmploymentType(str, enum.Enum):
    fte = "fte"
    contractor = "contractor"
    intern = "intern"


class AvailabilityStatus(str, enum.Enum):
    available = "available"
    away = "away"
    restricted = "restricted"


class SkillCategory(str, enum.Enum):
    technical = "technical"
    language = "language"
    domain = "domain"


class SkillLevel(str, enum.Enum):
    learning = "Learning"
    working = "Working"
    expert = "Expert"


class SkillSource(str, enum.Enum):
    inferred = "inferred"
    self_reported = "self"
    confirmed = "confirmed"
    certified = "certified"


class ProjectType(str, enum.Enum):
    project = "project"
    system = "system"
    function = "function"
    policy = "policy"


class ProjectClassification(str, enum.Enum):
    public = "public"
    internal = "internal"
    confidential = "confidential"


class CourseStatus(str, enum.Enum):
    """Training-course progress as the training system reports it.

    Stored at full four-value fidelity, always — user-facing copy collapses
    it to completed/not-completed (see CourseDisplayStatus), but the
    underlying value is what decides which reminder text an employee gets,
    so collapsing it in the database would throw away the only thing that
    distinguishes "you haven't started" from "you didn't pass".
    """

    not_started = "not_started"
    in_progress = "in_progress"
    failed = "failed"
    completed = "completed"


class CourseDisplayStatus(str, enum.Enum):
    """The two-value derivation shown to people. Never stored — always
    derived from CourseStatus at read time by display_status() below."""

    completed = "completed"
    not_completed = "not_completed"


# Human-readable copy for the derived status, kept next to the enum so the
# API can keep returning machine values (same convention as
# availability_status) without every renderer inventing its own wording.
DISPLAY_STATUS_LABEL: dict[CourseDisplayStatus, str] = {
    CourseDisplayStatus.completed: "Completed",
    CourseDisplayStatus.not_completed: "Not completed",
}


def display_status(status: CourseStatus) -> CourseDisplayStatus:
    """completed -> "completed"; not_started / in_progress / failed -> "not
    completed". The one place this mapping exists — nothing downstream
    reimplements it or compares raw statuses to build user-facing copy."""
    return (
        CourseDisplayStatus.completed
        if status is CourseStatus.completed
        else CourseDisplayStatus.not_completed
    )


class NotificationKind(str, enum.Enum):
    """Which of the two independent triggers produced a notification (see
    app/notifications.py). Both fire off the same status-change event; they
    differ in audience, in what they're allowed to say, and in when they
    fire — kept as distinct kinds so that stays inspectable after the fact."""

    employee_course_reminder = "employee_course_reminder"
    manager_course_report = "manager_course_report"
