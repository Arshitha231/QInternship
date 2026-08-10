from datetime import date

from pydantic import BaseModel, ConfigDict


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


class AskRequest(BaseModel):
    message: str


class SkillScarcityItem(BaseModel):
    """One entry in a skill_scarcity result — same shape whether it's a
    lookup for one named skill or the org-wide scarcest-skills scan."""

    model_config = ConfigDict(extra="forbid")

    skill: str
    expert_count: int
    working_count: int
    learning_count: int
    capable_count: int  # expert + working — genuine capability, not just familiarity
