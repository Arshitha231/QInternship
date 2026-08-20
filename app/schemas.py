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


class OrgUnitOut(BaseModel):
    """GET /org_units — a flat list, not a tree; the caller (today, the
    create-employee picker) already has to show every unit regardless of
    depth, and unit_type/parent_id are enough to label each one
    ("Platform Engineering — department") without the frontend re-deriving
    the hierarchy itself."""

    id: int
    name: str
    unit_type: str
    parent_id: int | None


class SkillOut(BaseModel):
    name: str
    category: str
    level: str
    source: str


class ProjectHistoryItem(BaseModel):
    # Which EmployeeProject row this is, so a caller who may EDIT project
    # history can address it (PUT/DELETE /people/{id}/projects/{project_id}).
    # Not gated: the project's NAME is already here for anyone who can see
    # project_history at all, and an opaque row id discloses strictly less
    # than the name it sits next to.
    project_id: int
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

    # This person's own one-line account of what they did on the project —
    # EmployeeProject.contribution, not Project.description (project_desc
    # above). Same visibility precedent as project_desc: EDITABLE gates who
    # may WRITE it (hr/work, see app.permissions and app.proposals'
    # FIELD_FOR_CHANGE_TYPE), but nothing narrows who may READ it beyond
    # project_history's own BASE_FIELDS gate — it was simply missing from
    # this model entirely, which is why accepting a document's contribution
    # proposal committed the row correctly but it never appeared on anyone's
    # profile.
    contribution: str | None = None


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
    # Whether this person manages anyone — a bare boolean, never the
    # identities, so it is safe on a bulk list where direct_reports is not.
    # Set on EVERY row (unlike direct_reports, which is single-match only),
    # because the org-tree UI needs it per card to decide whether to offer
    # an expand control, and finding out the other way round would be one
    # extra request per person on screen.
    #
    # Carries the same visibility gate as direct_reports and get_org_chain's
    # "down" direction (app.policy.can_see_direct_reports): in employee view
    # mode the key is absent, matching the fact that the downward chain it
    # advertises would come back empty there anyway. Advertising an expand
    # that expands to nothing is worse than not advertising it.
    has_reports: bool | None = None


class PersonWithProjects(BaseModel):
    """get_people_with_projects results -- the same always-visible base
    fields find_people's PersonSummary carries (so this reads as "a person
    summary plus their recent projects," not a separately-gated shape),
    plus each person's own recent project history. Built entirely from
    repeated get_person(id) calls (see app.people.get_people_with_projects)
    -- same enforce()/compute_visible_fields gate, same per-person audit
    row -- never a second, differently-filtered read.

    Fields mirror PersonDetail's own optionality (str | None), not
    PersonSummary's non-optional one, because that is genuinely what
    get_person returns them as. recent_projects is None (not []) for a
    caller who can't see project history at all -- the same absent-not-
    empty distinction PersonDetail.project_history already carries,
    inherited rather than re-decided here.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    full_name: str
    preferred_name: str | None = None
    job_title: str | None = None
    org_unit: str | None = None
    office: OfficeOut | None = None
    availability_status: str | None = None
    recent_projects: list[ProjectHistoryItem] | None = None


class PersonComparison(BaseModel):
    """compare_people results -- objective, already-visible attributes for a
    specific, caller-identified set of people, side by side. Same shape and
    provenance as PersonWithProjects (repeated get_person(id) calls, see
    app.people.compare_people -- same enforce()/compute_visible_fields gate,
    same per-person audit row), but no project history and no verdict field:
    there is deliberately no "rank" or "score" here. The out-of-scope rule
    against performance/ambition judgments ("who's the best candidate")
    still holds -- this tool only ever hands back facts to phrase side by
    side, never a winner. skills is None (not []) for a caller who can't
    see it at all, the same absent-not-empty distinction PersonDetail
    already carries.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    full_name: str
    job_title: str | None = None
    org_unit: str | None = None
    availability_status: str | None = None
    tenure_band: str | None = None
    skills: list[SkillOut] | None = None


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
    name_pronunciation: str | None = None
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
    salary_currency: str | None = None
    date_of_birth: date | None = None
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


class AmbiguousProjectMatch(BaseModel):
    """Several projects matched a name query and no single one is the
    obvious answer — returned by find_project_owner instead of an owner.

    A distinct type rather than a ProjectOwnerResult with an extra field,
    because "here is the owner" and "I don't know which project you mean"
    are different answers and shouldn't be distinguishable only by whether
    a list happens to be empty. The phrasing layer branches on the type.

    Same discipline as app.org_chart.resolve_person_name returning None for
    a duplicated employee name: answering a more specific question than the
    one asked is worse than admitting the ambiguity. It matters more here
    than it looks — "Migration" matches 16 of this directory's projects, and
    the previous implementation silently returned whichever sorted first.
    """

    model_config = ConfigDict(extra="forbid")

    query: str
    matches: list[str]


class PersonChoice(BaseModel):
    """One disambiguation candidate — enough to tell two people apart, and
    nothing more. Deliberately not a PersonSummary: this is a "which one did
    you mean" prompt, not a search result, and it must not become a way to
    read attributes about people the caller never actually asked for.
    Populated only from fields every role can already see.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    full_name: str
    job_title: str
    org_unit: str


class AmbiguousPersonMatch(BaseModel):
    """A name matched several people and no single one is the obvious
    answer — returned instead of a chain/profile, exactly like
    AmbiguousProjectMatch above does for projects.

    This is the type that stops the silent-wrong-person failure: "Anderson"
    matches five employees in this directory and "Mike" matches three
    preferred names, and the resolver used to pick whichever rapidfuzz
    ranked first. Naming the candidates costs one extra turn and is the only
    honest answer.
    """

    model_config = ConfigDict(extra="forbid")

    query: str
    matches: list[PersonChoice]


class UnknownPerson(BaseModel):
    """No active employee matched the name at all — distinct from
    AmbiguousPersonMatch (too many matched) and from an empty chain (the
    person exists, but has nobody above/below them, or that direction is
    restricted for this caller).

    Those three were previously indistinguishable: every one of them
    produced "Nobody found above them in the org chart (or that direction is
    restricted for your role)", which is a confidently wrong answer for the
    first two.
    """

    model_config = ConfigDict(extra="forbid")

    query: str


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

    `excerpt` is a sentence lifted verbatim from the matched project's own
    `description` — selected (by keyword overlap or embedding similarity),
    never generated — explaining why THAT project matched the described
    problem, as distinct from `reason`'s explanation of this PERSON's link
    to it. None when the project has no description, or nothing in it
    stood out from the query. See app/project_search.py's
    `_project_excerpts` for the selection logic.
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
    excerpt: str | None = None


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


class HistoryTurn(BaseModel):
    """One prior turn of a follow-up conversation, as returned by a
    previous /ask call -- a PLAN, never a result, matching the same rule
    saved sessions use (Conversational Assistant plan §2, "store the
    questions, not the answers"). `tool_call`/`arguments` are replayed
    through the ordinary enforce()-gated dispatcher fresh on every new
    turn (see tool_calling._history_messages) rather than trusted as
    given, so a value the client could tamper with never reaches the
    model's context unverified. `assistant_text` is carried as-is only
    for a turn that had no tool call (a clarifying question, an
    out-of-scope reply) -- connective language with no factual claim
    about a person, so nothing there needs re-checking."""

    message: str
    tool_call: str | None = None
    arguments: dict | None = None
    assistant_text: str | None = None


class AskRequest(BaseModel):
    message: str
    # "work" | "employee". Resolved server-side by resolve_view_mode, so an
    # employee-role caller sending "work" is still answered in employee mode.
    view_mode: str | None = None
    # Two ways a turn gets its prior context, not one -- see
    # app.assistant_conversations.open_or_continue for the exact rule:
    #
    #   conversation_id given: this turn's history comes from the
    #   server-side store (app.models.AssistantTurn rows on that
    #   conversation), and `history` below is ignored even if the client
    #   still sends one. conversation_id must already be this caller's own
    #   conversation -- a 404 otherwise, never a 403.
    #
    #   conversation_id absent: the pre-persistence path, kept working
    #   exactly as it always has -- `history` below is what's used for
    #   THIS turn. A conversation is still opened and this turn is still
    #   recorded to it server-side either way (its id comes back on the
    #   response), so an old client that never learns about
    #   conversation_id keeps working unmodified while still gaining
    #   persistence for free.
    #
    # Bounded to the last few turns by tool_calling.MAX_HISTORY_TURNS
    # regardless of which path supplied them or how long the list is.
    conversation_id: int | None = None
    history: list[HistoryTurn] = Field(default_factory=list)
    # Ids of the people currently on the caller's screen (e.g. the search
    # page's own result cards), sent only on the "search" surface so a
    # follow-up like "who is the best of these" can resolve "these" to real
    # ids instead of the model having no idea who is being asked about.
    # Untrusted as data: the route re-resolves each id server-side
    # (app.people.resolve_context_people) before it ever reaches the model,
    # never taking the client's word for who a name/id belongs to. Ignored
    # entirely by POST /prd/ask, which has no people tool to use it with.
    context_person_ids: list[str] = Field(default_factory=list)


class RecordCourseStatusRequest(BaseModel):
    """Body of the demo status-change endpoint. `status` is the four-value
    underlying status — this is the one inbound surface that speaks it,
    because it stands in for the training system telling us what happened."""

    status: Literal["not_started", "in_progress", "failed", "completed"]
    attempted_on: date | None = None
    completed_on: date | None = None


class LoginRequest(BaseModel):
    """Demo login body. Not a credential type worth modelling further — see
    app/demo_auth.py's module docstring for what this is and isn't."""

    email: str
    password: str


class UpdateBioRequest(BaseModel):
    bio: str = Field(max_length=2000)


class UpdateNamePronunciationRequest(BaseModel):
    # Free-text phonetic respelling ("nuh-VAY-uh"), not IPA. Same shape as
    # UpdateBioRequest: a full-replace PATCH, so an empty string is how the
    # owner clears a respelling they no longer want on file.
    name_pronunciation: str = Field(max_length=200)


# --- Self-service skills and languages (app/own_skills.py) -----------------
# One table behind both, split by category at render time, so these two
# request models serve the Skills card and the Languages card alike. Skill
# names are matched case-insensitively and through synonyms server-side, so
# the client never has to send a canonical spelling.

SkillLevelName = Literal["Learning", "Working", "Expert"]


class AddOwnSkillRequest(BaseModel):
    # 150 to match Skill.name's column width — an unrecognised name creates
    # the skills row, so this is the one request that can actually reach it.
    skill: str = Field(min_length=1, max_length=150)
    # Which card this came from, and therefore where a BRAND-NEW skill gets
    # filed. Ignored for a name that already exists — category belongs to
    # the skill, not to one person's holding of it — except that crossing
    # the Skills/Languages split is refused outright rather than silently
    # re-filed. See app/own_skills.py's SkillCategoryMismatch.
    category: Literal["technical", "domain", "language"] = "technical"
    level: SkillLevelName


class UpdateOwnSkillRequest(BaseModel):
    """Re-level a skill already held. Level is the only editable part: the
    name identifies the row, and category isn't a property of one person's
    holding. Correcting a name is a remove plus an add."""

    skill: str = Field(min_length=1, max_length=150)
    level: SkillLevelName


class UpsertProjectHistoryRequest(BaseModel):
    """IT's direct edit of one person's membership of one project.

    Same wire contract as UpdateEmployeeRequest: every field optional, the
    route sends only the keys actually supplied, and an explicit null
    clears. `{"end_date": null}` is how a project becomes current again,
    which must stay distinguishable from omitting end_date entirely.

    Creating a membership through this same model needs role and
    start_date, since both are NOT NULL on EmployeeProject -- that is
    enforced in app/writes.py rather than here, because whether this call
    creates or patches depends on whether the row already exists, which the
    wire shape cannot know.

    employee_id and project_id are deliberately absent: they identify the
    row (they are in the path), and moving a membership between people or
    projects is a delete plus a create, not a field edit.
    """

    model_config = ConfigDict(extra="forbid")

    role: str | None = Field(default=None, max_length=150)
    contribution: str | None = None
    start_date: date | None = None
    end_date: date | None = None


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
    # "restricted" is how a profile becomes invisible to everyone but HR
    # (app.permissions.is_record_visible) — the enforcement already existed;
    # this is what lets HR actually flip it. Not a general-purpose status
    # editor: the other two values (available/away) are here too, since one
    # field can't be write-only in one direction without genuinely being a
    # separate action (see app/writes.py's own note on why this stayed a
    # plain field rather than a dedicated restrict/unrestrict endpoint).
    availability_status: Literal["available", "away", "restricted"] | None = None
    # Reassigning someone's manager — needed before HR can deactivate a
    # manager who still has active direct reports (see
    # app.writes.deactivate_employee's block-until-reassigned rule).
    manager_id: str | None = None


class CreateEmployeeRequest(BaseModel):
    """POST /employees — HR, work mode. Deliberately a small required set;
    see app.writes' create section for why the rest (salary, date_of_birth,
    cost_centre, ...) is a follow-up PATCH /employees/{id} instead of a
    bigger form here.

    Staging only: this body describes a request for approval, not a person.
    Nothing lands in `employees` until the requester's resolved approver
    approves it (app.writes.request_creation)."""

    model_config = ConfigDict(extra="forbid")

    full_name: str = Field(max_length=200)
    job_title: str = Field(max_length=200)
    org_unit_id: int
    work_email: str = Field(max_length=320)
    employment_type: Literal["fte", "contractor", "intern"]
    preferred_name: str | None = Field(default=None, max_length=200)
    office_id: int | None = None
    manager_id: str | None = None
    work_phone: str | None = Field(default=None, max_length=50)
    hire_date: date | None = None
    # Not an employees column — becomes an official community_links row on
    # approval, the same shape auto_assign_mentors would have created. See
    # app.writes._apply_creation.
    mentor_id: str | None = None


class RejectActionRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=1000)


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


class FinalizeDocumentRequest(BaseModel):
    """Body of POST /docs/{id}/finalize — the "Update" action. Every id here
    gets accepted; every OTHER still-pending, employee-resolved proposal
    from this document gets rejected. An empty list is a valid, meaningful
    request — "reject everything, I don't want any of this document's
    suggestions" — not an error, same as unchecking every box would mean."""

    model_config = ConfigDict(extra="forbid")

    accept_ids: list[int] = Field(default_factory=list)


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


class ProjectListItem(BaseModel):
    """One row of the PRD page's project picker (GET /projects) -- id and
    name plus enough to decide what to show, never the full record."""

    model_config = ConfigDict(extra="forbid")

    id: int
    name: str
    type: str
    is_client_engagement: bool
    has_requirements: bool


class RequirementNoteIn(BaseModel):
    """A single qualitative requirement note being added -- the free-text
    field is deliberately named `note`, matching every other schema that
    carries one, so app.tool_calling._UNTRUSTED_FREE_TEXT_KEYS can exclude
    it from a model call by name alone."""

    model_config = ConfigDict(extra="forbid")

    note: str
    source_doc_id: int | None = None


class RequirementNoteOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str
    source_doc_id: int | None = None


class ProjectRequirementsOut(BaseModel):
    """What get_project_requirements (the PRD assistant's own tool) answers
    with for one resolved project -- skills and notes together, the
    combined shape a "what does this project need" question actually
    wants."""

    model_config = ConfigDict(extra="forbid")

    project_name: str
    description: str | None = None
    skills: list[ProjectSkillRequirementOut]
    notes: list[RequirementNoteOut]


class ProjectRequirementsSummaryItem(BaseModel):
    """One row of list_project_requirements_summary -- the PRD assistant's
    other tool, for "what have we captured so far" questions. Counts only,
    never the requirements themselves (that's what get_project_requirements
    is for, once a specific project is named)."""

    model_config = ConfigDict(extra="forbid")

    project_name: str
    skill_count: int
    note_count: int


# --- Cross-surface context (app/assistant_context.py) ---------------------
#
# What crosses between the search and PRD assistants: a tool name plus a
# resolved, re-checked reference -- never assistant_text (model-written
# prose) and never document/note prose, which never enters a stored turn's
# `arguments` in the first place. `ref_type`/`ref_id`, when set, are what
# recent_facts() re-resolves against the live database (visible_project /
# is_record_visible) before returning a fact -- a project reclassified
# confidential or a person deactivated since the turn happened is simply
# absent, the same freshness guarantee _history_messages() gives a
# replayed tool call. `kind` is a closed set, not a free string, so a
# renderer never has to guess what a new kind means.
class ConversationFact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal[
        "project_discussed", "skill_discussed", "person_discussed",
        "requirements_confirmed", "gap_checked",
    ]
    label: str
    ref_type: Literal["project", "skill", "person"] | None = None
    ref_id: str | None = None


# Pure Python, deterministic, computed alongside an answer -- never a tool
# the model is expected to call (see app/assistant_context.py's
# suggestion wrappers). `surface` is which assistant's OWN response this
# suggestion is attached to/rendered on ("search" for a suggestion computed
# by unified_search()'s wrapper and shown under a search answer, informed
# by PRD facts; "prd" for one computed by answer()'s PRD-profile wrapper
# and shown under a PRD answer, informed by search facts) -- not which
# surface the facts it's built from came from, which is always the other one.
class FollowUpSuggestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    surface: Literal["search", "prd"]
    kind: Literal["requirements_gap", "unfilled_skill"]
    label: str
    project_name: str | None = None
    skill: str | None = None
    minimum_level: str | None = None


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
    # Which pool this dependency's redundancy actually comes from --
    # orthogonal to `exposure` on the containing EngagementExposure, which
    # can land on the same severity band ("low") for two situations this
    # field tells apart: "project" (project_backup_count > 0, someone is
    # already here) reads very differently to HR than "org"
    # (project_backup_count == 0 but org_backup_count > 0, nobody here
    # today and a redeployment hasn't happened yet). "none" means neither
    # -- this is exactly the "high" severity case.
    redundancy_source: Literal["project", "org", "none"]
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
    hr_review_acknowledged_at: datetime | None
    hr_review_acknowledged_by: str | None


class SubmitAuthorizationRecordRequest(BaseModel):
    """Body of POST /continuity/employees/{id}/authorization-records. Enters
    a new record as pending_verification — it never becomes current on its
    own; see POST .../confirm."""

    model_config = ConfigDict(extra="forbid")

    authorization_type: Literal[
        "citizen", "permanent_resident", "cpt", "opt", "stem_opt", "h1b", "l1", "other",
    ]
    effective_from: date
    effective_until: date | None = None
    next_hr_review_date: date | None = None
    source_document_type: str | None = None
    internal_notes: str | None = None


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


# --- Community Graph (app/community_links.py) — private per-employee -----
# "who to contact for what" list. Every response here is scoped to the
# caller's own graph; there is no shape anywhere in this section that takes
# another employee's id as the subject of a query.

class CommunityLinkOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    owner_employee_id: str
    contact_employee_id: str
    role_label: str
    reason: str | None = None
    source: str
    office_id: int | None = None
    department_id: int | None = None
    is_mentor_link: bool
    created_at: datetime

    # --- resolved canonical roles (app/community_roles.py) ---------------
    # One of CANONICAL_ROLES for a resolved role; null for a personal link,
    # whose label is whatever the owner typed. The frontend keys its caption
    # and icon off this rather than parsing role_label.
    role_key: str | None = None
    contact_office_name: str | None = None
    contact_office_city: str | None = None
    # Whole kilometres from the owner's office to the contact's, and whether
    # that means this role was answered from another location because the
    # owner's own office has nobody in it. Both null/false for roles that
    # don't widen geographically (mentor, technical expert, project contact)
    # and for personal links.
    distance_km: int | None = None
    is_remote_fallback: bool = False


class CreateCommunityLinkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contact_employee_id: str
    role_label: str = Field(max_length=200)
    reason: str = Field(max_length=500)


class UpdateCommunityLinkRequest(BaseModel):
    """PATCH semantics — only supplied keys are touched, same convention as
    UpdateEmployeeRequest. Personal links only; enforced in
    app/community_links.py, not here."""

    model_config = ConfigDict(extra="forbid")

    role_label: str | None = Field(default=None, max_length=200)
    reason: str | None = Field(default=None, max_length=500)


class SuggestedOfficialLinkOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    office_id: int
    role_label: str
    candidate_employee_id: str
    status: str
    created_at: datetime
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None


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


# --- Dashboards (app/analytics.py) ----------------------------------------
#
# One set of shapes serving two dashboards. HR and a manager see the same
# fields; what differs is the SCOPE they were computed over, which is why
# every top-level response below carries a DashboardScope saying whose
# numbers these are. A manager's payload and HR's are structurally
# identical — the narrowing already happened server-side, in
# app/analytics.py's resolve_scope, and is not something the client is
# trusted to reproduce.
#
# Nothing here carries an INTERNAL_FIELDS value (salary, date of birth,
# cost centre, hire date). Aggregates over compensation would be a new
# disclosure with a new audience, and this feature does not ask for one.

class DashboardScope(BaseModel):
    """Whose data the response above it describes.

    `substituted` is true when the caller asked for one scope and policy
    gave them another — a manager sending an org_unit_id. Returned rather
    than silently corrected so the header can say "showing your team"
    instead of leaving a department selector pointing at data it didn't
    produce.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["org", "org_unit", "team"]
    label: str
    headcount: int
    org_unit_id: int | None = None
    org_unit: str | None = None
    manager_id: str | None = None
    substituted: bool = False


class OrgUnitOption(BaseModel):
    """One entry in the department selector. `headcount` is SUBTREE
    headcount — the number of people picking this option will actually
    scope to, not the unit's own rows, which for a division is zero."""

    model_config = ConfigDict(extra="forbid")

    id: int
    name: str
    unit_type: str
    parent_id: int | None
    headcount: int


class TrainingBuckets(BaseModel):
    """Course expectations, split four ways plus the rollup.

    The four buckets are mutually exclusive and sum to `expected`, so they
    render as one chart without double-counting; `incomplete` is the
    convenience rollup of the three non-completed ones and deliberately
    overlaps them. The unit counted is the (person, expected course) PAIR,
    never the person — see TrainingAnalytics.employee_count.
    """

    model_config = ConfigDict(extra="forbid")

    expected: int
    completed: int
    incomplete: int
    overdue: int
    due_soon: int
    outstanding: int
    compliance_pct: float


class TrainingBreakdown(BaseModel):
    """One row of a by-course or by-department split. `key` is the course
    code or the org unit id — whatever the caller filters back on."""

    model_config = ConfigDict(extra="forbid")

    key: str
    label: str
    buckets: TrainingBuckets
    employee_count: int


class TrainingAnalytics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: DashboardScope
    buckets: TrainingBuckets
    #: Distinct people behind `buckets.expected`, which counts pairs.
    employee_count: int
    #: How many of those pairs had NO reported status row and were read as
    #: not-started. Surfaced so the UI can qualify the figure rather than
    #: presenting an inference from absence as a measurement.
    no_record_count: int
    #: Pairs whose requirement records no deadline at all. These can never
    #: be overdue, and saying how many there are is what stops a healthy
    #: overdue count from looking like good compliance.
    no_deadline_count: int
    due_soon_days: int
    by_course: list[TrainingBreakdown]
    by_unit: list[TrainingBreakdown]
    #: Every course in scope, in stable name order — the filter's contents,
    #: as opposed to by_course which is sorted worst-first for display.
    courses: list[TrainingBreakdown]


class TrainingPersonRow(BaseModel):
    """One (person, course) pair in a drill-down list.

    `display_status` is the two-value derivation, never the four-value
    stored status: which of not_started / in_progress / failed somebody sits
    in decides the wording of the reminder they get and is not
    management-facing data. The profile makes the same collapse.
    """

    model_config = ConfigDict(extra="forbid")

    employee_id: str
    full_name: str
    job_title: str
    org_unit: str
    course_code: str
    course_name: str
    display_status: Literal["completed", "not_completed"]
    bucket: Literal["completed", "overdue", "due_soon", "outstanding"]
    due_on: str | None = None
    days_overdue: int | None = None
    has_record: bool = True


class TrainingRoster(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rows: list[TrainingPersonRow]
    total: int
    truncated: bool


class ReminderResult(BaseModel):
    """What actually went out, not what was asked for.

    The four counts differ for real reasons and are all reported: ids
    outside the caller's scope are dropped (`out_of_scope`), completed
    courses are never reminded about, and a same-day duplicate is
    suppressed (`skipped`). Reporting `requested` back as a success figure
    would claim sends that did not happen.
    """

    model_config = ConfigDict(extra="forbid")

    requested: int
    eligible: int
    sent: int
    recipients_notified: int
    out_of_scope: int
    skipped: int
    detail: str = ""


class SkillSupplyDemand(BaseModel):
    """One skill's supply (people in scope) against its demand (active
    projects in scope depending on it).

    `demand_basis` is never omitted. "declared" means a project recorded
    this skill as a requirement; "inferred" means no requirements were
    recorded for the project and the dependency was read off what its
    current members happen to know, which overcounts. Same declared-vs-
    inferred discipline as DeliveryDependency.source above, and for the same
    reason: a staffing decision made on an inferred number should know it.
    """

    model_config = ConfigDict(extra="forbid")

    skill_id: int
    skill: str
    category: str
    expert_count: int
    working_count: int
    learning_count: int
    #: Expert + Working — genuine capability, not just familiarity. The
    #: figure every verdict below is computed from.
    capable_count: int
    holder_count: int
    demand_project_count: int
    demand_basis: Literal["declared", "inferred", "none"]
    declared_project_count: int
    supply_per_project: float | None
    verdict: Literal["understaffed", "healthy", "overrepresented", "unused"]
    single_point_of_failure: bool
    coverage_pct: float
    maturity_pct: float
    maturity_label: str


class SkillHolder(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    full_name: str
    job_title: str
    org_unit: str
    level: Literal["Expert", "Working", "Learning"]


class SkillProjectUse(BaseModel):
    """One active project that depends on a skill. `capable_member_count`
    is how many of its current members hold that skill at Working or above
    — zero on a declared requirement is a staffing gap, not a rounding
    error."""

    model_config = ConfigDict(extra="forbid")

    project_id: int
    project_name: str
    basis: Literal["declared", "inferred"]
    member_count: int
    capable_member_count: int


class SkillDetail(BaseModel):
    """Everything behind one slice of the team-skills chart — the popup."""

    model_config = ConfigDict(extra="forbid")

    scope: DashboardScope
    skill_id: int
    skill: str
    category: str
    expert_count: int
    working_count: int
    learning_count: int
    capable_count: int
    holder_count: int
    #: Share of the scope's headcount holding this skill at any level.
    coverage_pct: float
    #: Level-weighted depth among the holders, 0-100, with its band name.
    #: Distinct from coverage: a skill three people hold at Expert is deep
    #: and barely covered, and the two must not be read off one number.
    maturity_pct: float
    maturity_label: str
    demand_project_count: int
    supply_per_project: float | None
    verdict: Literal["understaffed", "healthy", "overrepresented", "unused"]
    risk: Literal["high", "medium", "low"]
    #: The count that produced `risk`, in words. A severity without the
    #: number behind it is not checkable.
    risk_reason: str
    holders: list[SkillHolder]
    holders_truncated: bool
    projects: list[SkillProjectUse]


class ProjectCoverage(BaseModel):
    """One active project, and whether its DECLARED required skills are met
    by its current members.

    `requirements_recorded=False` means nothing was declared and no verdict
    is offered — `coverage_pct` is null and `risk` is "unknown". Inferring
    a project's requirements from its members' skills and then checking
    whether its members hold them is circular; it would report full
    coverage everywhere and mean nothing.
    """

    model_config = ConfigDict(extra="forbid")

    project_id: int
    project_name: str
    project_type: str
    is_client_engagement: bool
    member_count: int
    in_scope_member_count: int
    requirements_recorded: bool
    required_skill_count: int
    covered_skill_count: int
    coverage_pct: float | None
    gap_skills: list[str]
    #: Covered, but by exactly one person — met today, fragile tomorrow.
    single_cover_skills: list[str]
    risk: Literal["high", "medium", "low", "unknown"]


class WorkforceInsight(BaseModel):
    """One derived signal. Rule-based and deterministic — see
    app/analytics.py's `insights`, which explains why this section is not
    handed to a model.

    `evidence` carries the rows that triggered it so the claim in `title`
    can be checked against the table it came from, and the id lists let the
    UI turn an insight into a drill-down instead of a dead end.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal[
        "skill_shortage", "skill_concentration", "training_compliance",
        "project_staffing_gap", "profile_coverage", "bench_capacity",
    ]
    severity: Literal["high", "medium", "low"]
    title: str
    detail: str
    evidence: list[str] = Field(default_factory=list)
    skill_ids: list[int] = Field(default_factory=list)
    project_ids: list[int] = Field(default_factory=list)
    recommendation: str = ""


class InsightNarrative(BaseModel):
    """The dashboard's opening paragraph — see app/insight_narrative.py.

    `source` is not decoration. "model" means a language model ordered the
    already-computed findings into prose and every numeral it wrote was
    checked back against those findings; "derived" means a format string
    assembled the same facts, which is what runs with no model configured,
    on a failed call, and on a failed check. A demo screenshot should never
    be able to pass one off as the other.

    There is no third state: a summary is always returned, because the
    template always succeeds.
    """

    model_config = ConfigDict(extra="forbid")

    text: str
    source: Literal["model", "derived"]


class InsightReport(BaseModel):
    """GET /analytics/insights.

    The narrative and the findings travel together rather than as two calls:
    the summary is only meaningful against the exact list it was written
    over, and fetching them separately would let a re-render pair one
    scope's prose with another scope's cards.
    """

    model_config = ConfigDict(extra="forbid")

    summary: InsightNarrative
    insights: list[WorkforceInsight]


class DashboardOverview(BaseModel):
    """The headline row plus the rollups the sections below expand on.

    Every figure is recomputed from the same pass over the scope that the
    detailed sections use, so a tile and the table under it cannot
    disagree.
    """

    model_config = ConfigDict(extra="forbid")

    scope: DashboardScope
    headcount: int
    department_count: int
    division_count: int
    team_count: int
    #: People in scope who have at least one direct report. Null on scopes
    #: large enough that the per-person check isn't worth the query — an
    #: absent figure, never a wrong one.
    manager_count: int | None
    active_project_count: int
    client_engagement_count: int
    #: Distinct skills actually held by someone in scope.
    skill_count: int
    expert_count: int
    people_with_skills: int
    skill_profile_coverage_pct: float
    avg_skills_per_person: float
    understaffed_skill_count: int
    healthy_skill_count: int
    overrepresented_skill_count: int
    unused_skill_count: int
    single_point_skill_count: int
    training: TrainingBuckets
    training_employee_count: int
    due_soon_days: int


class SendRemindersRequest(BaseModel):
    """POST /analytics/training/reminders.

    Ids, not a filter. The caller selects rows from a roster they have
    already been shown, and sending the selection back is what makes the
    reminder match what was on screen — a filter re-evaluated server-side
    could quietly widen between the render and the click. Every id is still
    checked against the caller's resolved scope, so the explicit list is a
    narrowing, never an authorization.
    """

    model_config = ConfigDict(extra="forbid")

    employee_ids: list[str] = Field(min_length=1, max_length=500)
    #: Restrict to one course. Omitted, every outstanding course for each
    #: selected person is reminded — one notification per (person, course),
    #: which is how their inbox already works.
    course_code: str | None = None


# --- Skill bridges (app/skill_routes.py) ----------------------------------
#
# The shortest introduction chain to somebody who has a skill you lack. Built
# entirely from BASE_FIELDS (skills, project membership) so a route discloses
# nothing the traveller could not look up person by person -- see that
# module's docstring for why the reporting line is deliberately not an edge.

class SkillTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill_id: int
    skill: str
    category: str
    #: People who hold it at Working or above, across the whole directory --
    #: not just the ones reachable from the asker.
    capable_count: int


class SkillRouteHop(BaseModel):
    """One step along the chain, and — the part that matters — WHY that step
    exists. A path with unlabelled edges tells you to go talk to a stranger;
    "you are both on Payroll Annual Planning" tells you how to open."""

    model_config = ConfigDict(extra="forbid")

    person: PersonRef
    job_title: str
    via_kind: Literal["project", "team", "past_project", "skill"]
    #: The project or skill the two people have in common.
    via: str


class SkillRoute(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: PersonRef
    job_title: str
    level: Literal["Expert", "Working"]
    #: In order, starting from the first person the asker already has a
    #: connection to and ending at the target. Never empty.
    hops: list[SkillRouteHop]


class SkillRouteResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: None when the requested name matched no skill at all -- reported as
    #: unresolved rather than silently returning zero routes, which would
    #: look like "nobody has it".
    skill: SkillTarget | None
    requested: str
    from_person: PersonRef
    #: True when the asker already holds it at Working or above. Routing
    #: somebody to themselves is not an answer.
    already_capable: bool
    routes: list[SkillRoute]
    #: Capable holders no chain reaches within the hop limit. "Three people
    #: have this and none are connected to you" is a real answer, and an
    #: empty list with no count would read as "nobody has it".
    unreachable_holder_count: int


class SuggestedSkill(BaseModel):
    """An entry in the empty state. Always carries its own reason: "learn
    this" with nothing attached is horoscope advice."""

    model_config = ConfigDict(extra="forbid")

    skill_id: int
    skill: str
    capable_count: int
    reason: str


# --- Workforce Intelligence reports (app/workforce_reports.py) ------------
#
# A natural-language question answered as a structured, evidence-backed
# report. The model plans WHICH analyses to run and writes the prose; it
# never queries anything and never decides who may see what. Retrieval goes
# through app/analytics.py's resolve_scope, the same gate the dashboard
# uses, so the facts the model is shown are already permission-filtered.
#
# Every claim-bearing field carries `evidence`. That is not decoration: it
# is what makes a generated sentence checkable against the table it came
# from, and what the UI turns into a click-through.

AnalysisType = Literal["skill_gap", "skill_scarcity", "training", "project_coverage"]


class ReportEvidence(BaseModel):
    """One checkable fact behind a finding, with the ids needed to open it.

    `label` is prose a reader can verify at a glance ("Terraform: 2 Expert,
    8 Working, 14 Learning"). The ids are how the UI turns that into a
    drill-down into the existing skill / project / roster views -- they are
    deliberately NOT shown to the model (see the payload builder), so a
    fabricated id is impossible rather than merely unlikely.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["skill", "project", "training", "department"]
    label: str
    skill_id: int | None = None
    project_id: int | None = None
    course_code: str | None = None
    org_unit_id: int | None = None


class ReportFinding(BaseModel):
    """A single statement in the report, with what backs it.

    `severity` is assigned by the deterministic analysis, never by the
    model: how bad something is follows from the counts, and letting prose
    decide it would make the badge disagree with the table.
    """

    model_config = ConfigDict(extra="forbid")

    title: str
    detail: str
    severity: Literal["high", "medium", "low", "info"] = "info"
    evidence: list[ReportEvidence] = Field(default_factory=list)


class ReportSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    heading: str
    findings: list[ReportFinding] = Field(default_factory=list)


class WorkforceReport(BaseModel):
    """The whole answer.

    `analyses` records which analysis types actually ran, so a reader can
    tell "the training section is empty because nothing is overdue" from
    "the training section is empty because the question never asked for
    it". `narrative_source` says whether the prose was model-written (and
    therefore numeral-checked) or assembled deterministically.
    """

    model_config = ConfigDict(extra="forbid")

    title: str
    query: str
    scope: DashboardScope
    analyses: list[AnalysisType]
    #: Analysis types the query asked for that this build cannot serve.
    #: Reported rather than silently dropped.
    unsupported: list[str] = Field(default_factory=list)
    executive_summary: str
    narrative_source: Literal["model", "derived"]
    strengths: ReportSection
    skill_gaps: ReportSection
    risks: ReportSection
    training_insights: ReportSection
    project_insights: ReportSection
    recommendations: ReportSection
    #: Everything the report drew on, deduplicated -- the reader's audit
    #: trail, and the UI's index for click-through.
    evidence: list[ReportEvidence] = Field(default_factory=list)


class WorkforceReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=500)
