from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class PersonRef(BaseModel):
    id: str
    full_name: str


class OfficeOut(BaseModel):
    id: int
    name: str
    city: str
    country: str


class SkillOut(BaseModel):
    name: str
    category: str
    level: str
    source: str


class ProjectHistoryItem(BaseModel):
    project_name: str
    project_type: str
    role: str
    start_month: str  # "2024-03" — month/year only, never an exact date
    end_month: str | None  # None means still current
    current: bool

    # Visible to every role/view_mode — it's in permissions.BASE_FIELDS, same
    # as project_history itself. Still `str | None = None` (left unset, not
    # None) rather than a plain required field: the field-presence mechanism
    # is shared with every other conditionally-visible field on this model,
    # and a future restriction narrowing it again should only need a
    # permissions.py change, not a schema change too. Left unset means the
    # routes' exclude_unset serialization drops the key entirely rather than
    # emitting null; Pydantic applies exclude_unset recursively, so nesting it
    # here keeps the same absent-not-null guarantee the top-level fields have.
    project_desc: str | None = None


class TrainingStatusItem(BaseModel):
    """One course on a person's profile.

    Carries the two-value derivation only. The underlying four-value status
    (not_started / in_progress / failed / completed) is deliberately absent
    from every API response: it exists in the database and drives the
    wording of the employee's own reminder, but "didn't pass" is not
    something the directory shows anyone, including HR.

    `expected` is what separates "hasn't done a course we require" from
    "did a course nobody required" — without it a bare not-completed list
    can't be read.
    """

    model_config = ConfigDict(extra="forbid")

    course_code: str
    course_name: str
    display_status: str  # "completed" | "not_completed"
    display_label: str  # "Completed" | "Not completed" — the copy, server-owned
    expected: bool
    attempted_month: str | None = None  # "2026-04" — month granularity, as elsewhere
    completed_month: str | None = None
    source: str  # which provider answered: "synthetic" | "training_api"


class PersonSummary(BaseModel):
    """find_people results. The base fields are always the same always-visible
    set — no ABAC/RBAC gated data — so a bulk list can never leak more than a
    single lookup would.

    manager/delegate/direct_reports are the one exception, and only ever set
    when the search resolved to exactly one person (never on a multi-result
    list, which is what keeps the "no gated data in bulk" guarantee intact for
    everything else). manager/delegate are visible to all, same as on
    get_person; direct_reports carries the same downward-visibility
    restriction as get_org_chain's "down" direction (manager/hr only) — the
    route serializes with exclude_unset=True, so a caller who can't see it
    gets the key genuinely absent, not null.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    full_name: str
    preferred_name: str | None = None
    job_title: str
    org_unit: str
    office: OfficeOut | None = None
    availability_status: str
    manager: PersonRef | None = None
    delegate: PersonRef | None = None
    direct_reports: list[PersonRef] | None = None


class PersonDetail(BaseModel):
    """get_person result. Only fields the caller is actually allowed to see
    are ever set on the instance; the route serializes with
    exclude_unset=True so anything not set is genuinely ABSENT from the
    response body, not present-as-null.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    full_name: str
    preferred_name: str | None = None
    job_title: str | None = None
    org_unit: str | None = None
    work_email: str | None = None
    work_phone: str | None = None
    slack_handle: str | None = None
    effective_timezone: str | None = None
    employment_type: str | None = None
    photo_url: str | None = None
    office: OfficeOut | None = None
    manager: PersonRef | None = None
    delegate: PersonRef | None = None
    availability_status: str | None = None
    away_until_month: str | None = None
    tenure_band: str | None = None
    bio: str | None = None
    skills: list[SkillOut] | None = None
    languages: list[SkillOut] | None = None
    project_history: list[ProjectHistoryItem] | None = None
    # Absent for a caller who can't see it, AND absent when the provider
    # couldn't answer — the two are indistinguishable from outside on
    # purpose, same redact-never-reject shape as every other gated field.
    training_status: list[TrainingStatusItem] | None = None
    hire_date: date | None = None
    cost_centre: str | None = None
    personal_mobile: str | None = None
    # HR or the person themselves — never the manager. Serialized as a string
    # so the exact decimal survives the trip: JSON numbers are IEEE 754
    # doubles in most clients, and pay is not a value to hand to a float.
    salary: str | None = None
    date_of_birth: date | None = None
    
    salary_currency: str | None = None
    
    linkedin_profile: str | None = None


class OrgChainNode(BaseModel):
    """One entry in a get_org_chain result. depth=1 is a direct manager (up)
    or direct report (down); depth increases moving further from the root.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    full_name: str
    job_title: str
    org_unit: str
    depth: int
    availability_status: str
    delegate: PersonRef | None = None
    has_reports: bool


class ProjectOwnerResult(BaseModel):
    """find_project_owner result. Covers project | system | function |
    policy uniformly — one lookup answers for anything the org owns."""

    model_config = ConfigDict(extra="forbid")

    project_name: str
    project_type: str
    classification: str
    owner_id: str
    owner_name: str


class MentorCandidate(BaseModel):
    """One find_mentor result. `reason` is always populated — the system
    finds people who match requirements, it never claims to rank the "best"
    candidate, since that depends on performance and ambition, which
    aren't in the directory."""

    model_config = ConfigDict(extra="forbid")

    id: str
    full_name: str
    job_title: str
    level: str
    reason: str


class ProblemExpert(BaseModel):
    """One find_experts result (Mode 3, app/project_search.py) — a person
    reached by hopping from a project that matched a described problem.

    `reason` states only what the assignment record shows ("works on
    Project Atlas as Tech Lead"), never that this person is the best one to
    ask: same discipline as MentorCandidate above and continuity.py's
    dependency reasons. `retrieval` records which arms actually ran
    ("semantic+keyword" / "keyword"), so a keyword-only answer — which is
    what happens before the corpus has been embedded — is never presented
    as a semantic match.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    full_name: str
    job_title: str
    org_unit: str
    availability_status: str
    project_id: int
    project_name: str
    role: str
    current: bool
    reason: str
    retrieval: str


class SkillGapItem(BaseModel):
    """One entry in a skill_gap result — coverage for one requested skill."""

    model_config = ConfigDict(extra="forbid")

    skill: str
    recognized: bool  # False if the skill name didn't resolve to anything indexed
    expert_count: int
    working_count: int
    learning_count: int
    gap: bool  # no Working/Expert holders at all


class NotificationOut(BaseModel):
    """One notification, as its recipient sees it. Only ever returned to the
    recipient themself — see the route in app/main.py."""

    model_config = ConfigDict(extra="forbid")

    id: int
    kind: str
    subject_person: PersonRef  # who it's about; equals the recipient on own reminders
    course_name: str
    display_status: str
    body: str
    levels_up: int
    created_at: datetime


class AskRequest(BaseModel):
    message: str
    # "work" | "employee". Resolved server-side by resolve_view_mode, so an
    # employee-role caller sending "work" is still answered in employee mode.
    view_mode: str | None = None


class RecordCourseStatusRequest(BaseModel):
    """Body of the demo status-change endpoint. `status` is the four-value
    underlying status — this is the one inbound surface that speaks it,
    because it stands in for the training system telling us what happened."""

    status: Literal["not_started", "in_progress", "failed", "completed"]
    attempted_on: date | None = None
    completed_on: date | None = None


class UpdateBioRequest(BaseModel):
    bio: str = Field(max_length=2000)


class UpdateEmployeeRequest(BaseModel):
    """HR's internal-field edit. Every field optional, and the route sends
    only the keys actually supplied (model_dump(exclude_unset=True)) — so
    `{"salary": null}` clears the salary while omitting the key leaves it
    untouched. extra="forbid" so a typo'd or non-editable field name is a
    422 rather than a silently ignored no-op that looks like it worked.

    Which of these the caller may actually write is not decided here: it's
    the EDITABLE table in app/permissions.py, checked by app/writes.py. This
    model only describes the wire shape.
    """

    model_config = ConfigDict(extra="forbid")

    full_name: str | None = Field(default=None, max_length=200)
    preferred_name: str | None = Field(default=None, max_length=200)
    job_title: str | None = Field(default=None, max_length=200)
    work_email: str | None = Field(default=None, max_length=320)
    work_phone: str | None = Field(default=None, max_length=50)
    salary: str | float | None = None  # str, for the same precision reason the response uses str
    salary_currency: str | None = Field(default=None, max_length=3)
    date_of_birth: date | None = None
    hire_date: date | None = None
    cost_centre: str | None = Field(default=None, max_length=50)
    employment_type: Literal["fte", "contractor", "intern"] | None = None
    linkedin_profile: str | None = Field(default=None, max_length=500)


class ProjectDescriptionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = Field(max_length=4000)


class ReassignProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    employee_id: str


class CorrectProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Free text from the reviewer, sent back through the function-calling
    # loop as data. Treated as untrusted content, same as the document.
    instruction: str = Field(max_length=2000)


class EditProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # The reviewer's own value, committed as-is — not sent through the
    # model at all (that's what /correct is for). Shape depends on the
    # proposal's change_type (e.g. {"skill": "..."} vs
    # {"project": "...", "contribution": "..."}), which is why this is a
    # bare object rather than a typed model: the wire contract is "same
    # keys as GET /proposed_changes' proposed_value for this row."
    edited_value: dict = Field(min_length=1)


class ResolveSubjectRequest(BaseModel):
    """Body of POST /doc_subject_matches/{id}/resolve — exactly one of
    employee_id or new_hire, enforced in app.proposals.resolve_subject
    rather than here, so the same validation applies to every caller of
    that function, not just this route."""

    model_config = ConfigDict(extra="forbid")

    employee_id: str | None = None
    new_hire: bool = False


class BulkProposalRequest(BaseModel):
    """Body shared by /proposed_changes/bulk_accept and .../bulk_reject.
    Exactly one selector — an explicit id list, or a doc_id/employee_id
    filter — is required; app.proposals._bulk_targets is where that's
    actually enforced, so a future third caller (a scheduled sweep, say)
    gets the same rule for free."""

    model_config = ConfigDict(extra="forbid")

    ids: list[int] | None = None
    doc_id: int | None = None
    employee_id: str | None = None


class SkillScarcityItem(BaseModel):
    """One entry in a skill_scarcity result — same shape whether it's a
    lookup for one named skill or the org-wide scarcest-skills scan."""

    model_config = ConfigDict(extra="forbid")

    skill: str
    expert_count: int
    working_count: int
    learning_count: int
    capable_count: int  # expert + working — genuine capability, not just familiarity


# --- Project skill requirements (app/project_skills.py) -------------------
# What a project's delivery actually needs, as recorded by whoever owns it
# or HR. Not sensitive — visible to anyone who can see the project at all,
# same confidentiality rule as everywhere else. Write access is narrower:
# the project's owner, or HR (app/project_skills.py's own check).

class ProjectSkillRequirementIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill: str
    minimum_level: Literal["Learning", "Working", "Expert"] = "Working"


class ProjectSkillRequirementOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill: str
    minimum_level: str


# --- Staffing Continuity Intelligence (app/continuity.py) — HR-only ------
#
# The unit these describe is the client ENGAGEMENT (a project), not the
# employee — "Project Apollo — High continuity exposure", never
# "<person> — HIGH RISK". Every one of these is HR_ONLY in practice (gated
# by app/continuity.py's own caller.role check, not by app/registry.py —
# see that file's DERIVED_HR comment for why this deliberately isn't
# routed through the registry/policy-engine pipeline at all).

class DeliveryDependency(BaseModel):
    """One capability or role a specific employee provides on a specific
    client engagement. MVP covers 2 of the design doc's 6 dependency types
    — skill and project_role — chosen because they're the only two this
    schema can answer without guessing at data that doesn't exist yet
    (there's no industry/domain model, no project-required-skill mapping,
    no certification-requirement field on a project)."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["skill", "project_role"]
    name: str  # e.g. "Power BI", or the role value ("Lead")
    project_id: int
    employee: PersonRef
    project_backup_count: int  # others already on this engagement with the same dependency
    org_backup_count: int      # others anywhere else in the org, excluding project members
    # "declared": a real recorded fact -- either a ProjectSkillRequirement
    # row (app/project_skills.py) the employee meets, or the project_role
    # dependency (employee_projects.role is always ground truth).
    # "inferred": no ProjectSkillRequirement list exists for this project,
    # so this is the fallback heuristic -- any Working/Expert skill the
    # employee happens to hold while staffed here, whether or not the
    # engagement actually needs it. Surfaced so this number is never
    # presented as more precise than it is.
    source: Literal["declared", "inferred"]


class BackupCandidate(BaseModel):
    """A potential internal capability match for one thin dependency —
    never a "replace X with Y" framing, always factual matching evidence."""

    model_config = ConfigDict(extra="forbid")

    id: str
    full_name: str
    matching_evidence: str


class EngagementExposure(BaseModel):
    """One client engagement's continuity exposure. `reasons` is always
    factual/mechanical (what intersects, what's thin) — never speculative
    about the employee ("may lose authorization"); the service doesn't
    know that and never claims to."""

    model_config = ConfigDict(extra="forbid")

    project_id: int
    project_name: str
    exposure: Literal["none", "low", "medium", "high"]
    rule_version: int
    reasons: list[str]
    intersecting_review_count: int
    # The nearest (most urgent) intersecting review on this engagement, and
    # how much of the assignment remains after it — the Continuity Planning
    # Window, the actionable metric, not a flat expiration date. Both None
    # when nothing intersects (exposure == "none").
    days_until_hr_review: int | None = None
    days_of_assignment_remaining_after_review: int | None = None
    dependencies: list[DeliveryDependency]
    backups: dict[str, list[BackupCandidate]]  # keyed by DeliveryDependency.name


class ContinuityOverview(BaseModel):
    """GET /continuity/exposure — organization-wide summary. Deliberately
    does NOT lead with "employees with visa events" as the headline metric
    — the feature's purpose is client delivery continuity, not a visa-date
    ticker."""

    model_config = ConfigDict(extra="forbid")

    rule_version: int
    window_days: int
    by_severity: dict[str, int]  # "high"/"medium"/"low" -> engagement count, within window_days
    engagements: list[EngagementExposure]


class AuthorizationRecordOut(BaseModel):
    """One WorkAuthorizationRecord, as HR sees it on an employee's history."""

    model_config = ConfigDict(extra="forbid")

    id: int
    authorization_type: str
    effective_from: date
    effective_until: date | None
    next_hr_review_date: date | None
    verification_status: str
    is_current: bool
    verified_at: datetime | None


class HrReviewQueueItem(BaseModel):
    """GET /continuity/review-queue — one row: an employee with a current,
    HR-verified work-authorization record and a scheduled
    next_hr_review_date. This is the original HR pain point ("who is
    nearing a review date"), independent of whether that review happens to
    intersect a client engagement — unlike ContinuityOverview.engagements,
    which only ever surfaces someone whose review DOES intersect
    something. An employee can appear here with engagements_affected == 0
    (no client-engagement consequence at all) and that is itself the
    answer, not a missing case — see the design doc's Case D."""

    model_config = ConfigDict(extra="forbid")

    employee: PersonRef
    current_record: AuthorizationRecordOut
    days_until_hr_review: int
    engagements_affected: int
    highest_exposure: Literal["none", "low", "medium", "high"]


class EmployeeContinuityDetail(BaseModel):
    """GET /continuity/employees/{id} — HR drill-down. `engagements` lists
    EVERY one of this employee's current client-engagement assignments,
    including ones where the review does NOT intersect (exposure="none") —
    unlike ContinuityOverview.engagements, nothing here is filtered out.
    That's deliberate: an assignment ending before the next review is
    exactly as informative to HR as one that doesn't."""

    model_config = ConfigDict(extra="forbid")

    employee: PersonRef
    current_record: AuthorizationRecordOut | None
    history: list[AuthorizationRecordOut]
    engagements: list[EngagementExposure]
