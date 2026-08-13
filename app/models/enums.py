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
    """Which trigger produced a notification (see app/notifications.py).

    The first two fire off the same course-status-change event and differ in
    audience, in what they're allowed to say, and in when they fire. The last
    two are date-driven rather than event-driven: nothing changes in the
    database on someone's birthday, so a sweep has to go looking. Kept as
    distinct kinds so which trigger fired stays inspectable after the fact.
    """

    employee_course_reminder = "employee_course_reminder"
    manager_course_report = "manager_course_report"
    birthday_reminder = "birthday_reminder"
    work_anniversary_reminder = "work_anniversary_reminder"


# Years of service worth telling HR about: the first one, then every fifth.
# Unbounded on purpose — a 45-year anniversary is rarer and more worth
# marking, not less, so this is a rule rather than a list that silently stops.
def is_milestone_year(years: int) -> bool:
    return years == 1 or (years >= 5 and years % 5 == 0)
