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


class RecordCourseStatusRequest(BaseModel):
    """Body of the demo status-change endpoint. `status` is the four-value
    underlying status — this is the one inbound surface that speaks it,
    because it stands in for the training system telling us what happened."""

    status: Literal["not_started", "in_progress", "failed", "completed"]
    attempted_on: date | None = None
    completed_on: date | None = None


class UpdateBioRequest(BaseModel):
    bio: str = Field(max_length=2000)


class SkillScarcityItem(BaseModel):
    """One entry in a skill_scarcity result — same shape whether it's a
    lookup for one named skill or the org-wide scarcest-skills scan."""

    model_config = ConfigDict(extra="forbid")

    skill: str
    expert_count: int
    working_count: int
    learning_count: int
    capable_count: int  # expert + working — genuine capability, not just familiarity
