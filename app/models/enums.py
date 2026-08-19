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


class WorkAuthorizationType(str, enum.Enum):
    """Legal basis for working, not employment/payroll classification --
    deliberately kept separate from EmploymentType (fte/contractor/intern)
    per the continuity design doc: HR mentioned W-2 alongside CPT/OPT in the
    same breath, but they are different HR concepts and conflating them into
    one field would make neither queryable correctly."""

    citizen = "citizen"
    permanent_resident = "permanent_resident"
    cpt = "cpt"
    opt = "opt"
    stem_opt = "stem_opt"
    h1b = "h1b"
    l1 = "l1"
    other = "other"


class VerificationStatus(str, enum.Enum):
    """Lifecycle of a single WorkAuthorizationRecord row. The continuity
    engine only ever reads a record that is both is_current=True AND
    verified here -- a pending_verification row (e.g. employee-submitted,
    not yet HR-confirmed) must never affect organizational analysis.

    `rejected` is distinct from `expired_record` on purpose: expired means
    a record WAS accurate and its window lapsed, a temporal fact; rejected
    means HR reviewed a pending submission and it was never accurate.
    Conflating them would corrupt any future review-queue logic that
    surfaces expired_record rows for renewal follow-up. Rejected rows are
    kept, not deleted -- same reasoning as ProposedChangeStatus.rejected."""

    pending_verification = "pending_verification"
    verified = "verified"
    superseded = "superseded"
    expired_record = "expired_record"
    rejected = "rejected"


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


class DocumentType(str, enum.Enum):
    """What kind of document was uploaded, decided by the pipeline's first
    step before any extraction runs. Everything downstream — which typed
    function the model is offered, which change_types get produced — is
    conditioned on this one classification."""

    project_doc = "project_doc"
    resume = "resume"


class ChangeType(str, enum.Enum):
    """What a proposed_changes row is proposing to add, at the granularity
    of one editable field on one target — never a bundle.

    Deliberately narrow, same reasoning this enum's predecessor
    (ProposedFieldType) carried: the extraction step may only propose kinds
    of thing a document actually evidences. It cannot propose a salary, a
    title, or a manager, because a document saying so is not evidence that
    it's true.

      skill           an EmployeeSkill row (new or, on accept, a no-op if
                       the person already holds it — never downgraded)
      contribution    EmployeeProject.contribution — the prose describing
                       what this person did on a project, distinct from...
      project_entry   ...the EmployeeProject membership itself (which
                       project, what role, when) — split apart from
                       contribution so a reviewer can accept "they were on
                       this project" while rejecting a contribution
                       sentence that overreached, or vice versa
    """

    skill = "skill"
    contribution = "contribution"
    project_entry = "project_entry"


class ResolutionStatus(str, enum.Enum):
    """doc_subject_matches.resolution_status — the disambiguation step's
    own state, independent of whether any proposed_changes row attached to
    it has been reviewed yet.

    new_hire_candidate is not a form of "resolved": it means a human looked
    at the candidate list and confirmed nobody plausible exists, which is
    the case that should get flagged to HR to create the employee record
    first. It is its own terminal state, not resolved with a null
    resolved_employee_id — a null employee_id could otherwise be
    misread as "hasn't been looked at yet" instead of "looked at, and the
    answer is nobody."
    """

    unresolved = "unresolved"
    resolved = "resolved"
    new_hire_candidate = "new_hire_candidate"


class ProposedChangeStatus(str, enum.Enum):
    """Review state. Only `accepted` has ever touched a real table.

    `edited` is distinct from `accepted` on purpose: both mean the change is
    live, but `edited` records that a human changed the content before
    committing it, which is the signal for whether the extraction step is
    actually any good. Collapsing them would throw away the only measure of
    that.

    `rejected` rows are kept rather than deleted, for the same reason: a
    proposal nobody accepted is the most useful thing to look at when the
    extraction prompt is next revised.
    """

    pending = "pending"
    accepted = "accepted"
    edited = "edited"
    rejected = "rejected"


# The statuses whose content has been committed to the real tables and is
# therefore searchable. Everything else is invisible to retrieval — see
# app/proposals.py. One definition, so "is this live?" can never be answered
# two different ways in two different places.
LIVE_PROPOSAL_STATUSES: frozenset[ProposedChangeStatus] = frozenset(
    {ProposedChangeStatus.accepted, ProposedChangeStatus.edited}
)


class NotificationKind(str, enum.Enum):
    """Which trigger produced a notification (see app/notifications.py).

    The first two fire off the same course-status-change event and differ in
    audience, in what they're allowed to say, and in when they fire. The
    middle two are date-driven rather than event-driven: nothing changes in
    the database on someone's birthday, so a sweep has to go looking. The
    last is also a sweep, over WorkAuthorizationRecord.next_hr_review_date
    rather than a birthday/anniversary — see notify_upcoming_hr_reviews.
    Kept as distinct kinds so which trigger fired stays inspectable after
    the fact.
    """

    employee_course_reminder = "employee_course_reminder"
    manager_course_report = "manager_course_report"
    birthday_reminder = "birthday_reminder"
    work_anniversary_reminder = "work_anniversary_reminder"
    hr_review_reminder = "hr_review_reminder"
    # app.writes' restrict/deactivate approval flow — a maker-checker
    # control, not a course or date trigger, but the same "the row is the
    # delivery" reasoning applies, so it shares this table rather than a
    # second one.
    action_approval_requested = "action_approval_requested"
    action_approved = "action_approved"
    action_rejected = "action_rejected"


class EmployeeActionType(str, enum.Enum):
    """What app.writes' maker-checker flow is being asked to do.

    `create` joined the other two rather than staying single-actor as
    originally reasoned ("the lower-risk, additive direction"): adding a
    person is what mints a real identity in the directory, and a fabricated
    one is not cheaply undone — deactivating it later leaves the record, its
    audit trail, and anything already linked to it in place. reactivate_
    employee is still single-actor, and genuinely is the low-risk undo
    direction: it can only restore somebody the same two-person control
    already approved removing.

    `create` is also the only member whose request has no target_employee_id
    — there is no employee to point at until the approval lands. See
    EmployeeActionRequest.payload.
    """

    restrict = "restrict"
    deactivate = "deactivate"
    create = "create"


class EmployeeActionStatus(str, enum.Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"


# Years of service worth telling HR about: the first one, then every fifth.
# Unbounded on purpose — a 45-year anniversary is rarer and more worth
# marking, not less, so this is a rule rather than a list that silently stops.
def is_milestone_year(years: int) -> bool:
    return years == 1 or (years >= 5 and years % 5 == 0)


class CommunityLinkSource(str, enum.Enum):
    """community_links.source. An official row is read-only to the employee
    (app/community_links.py's _load_owned_personal) regardless of role — an
    employee may only add alongside, never edit or override one. A mentor
    link is created official and flips to personal at expiration; see
    CommunityLink.is_mentor_link."""

    official = "official"
    personal = "personal"


class SuggestedLinkStatus(str, enum.Enum):
    """suggested_official_links.status — HR's review queue for office/role
    -> candidate mappings bootstrapped from existing office/job-title data.
    Same staging discipline as ProposedChangeStatus: nothing becomes a real
    community_links edge until an explicit `confirmed`."""

    pending = "pending"
    confirmed = "confirmed"
    rejected = "rejected"
