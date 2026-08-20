// Mirrors app/schemas.py. Fields are optional throughout because the API
// serializes with response_model_exclude_unset=True — a field the caller
// can't see is genuinely absent from the JSON, not null.

export interface OfficeOut {
  id: number;
  name: string;
  city: string;
  country: string;
}

export interface OrgUnitOut {
  id: number;
  name: string;
  unit_type: string;
  parent_id: number | null;
}

export interface PersonRef {
  id: string;
  full_name: string;
}

export interface PersonSummary {
  id: string;
  full_name: string;
  preferred_name?: string;
  job_title: string;
  org_unit: string;
  office?: OfficeOut;
  availability_status: string;
  manager?: PersonRef;
  delegate?: PersonRef;
  direct_reports?: PersonRef[];
  // Whether this person manages anyone. Absent (not false) in employee view
  // mode, where the server withholds the downward chain -- see
  // app/schemas.py's PersonSummary. TeamGraph uses it to decide which
  // roster cards get an expand control.
  has_reports?: boolean;
}

export interface SkillOut {
  name: string;
  category: string;
  level: string;
  source: string;
}

export interface ProjectHistoryItem {
  // Addresses the EmployeeProject row for PUT/DELETE
  // /people/{id}/projects/{project_id}. Always present -- it's ungated on
  // the backend, same as project_name.
  project_id: number;
  project_name: string;
  project_type: string;
  role: string;
  start_month: string;
  end_month: string | null;
  current: boolean;
  // Visible to every role/view_mode that can see project_history at all --
  // EDITABLE gates who may WRITE this (hr/work only), not who may read it.
  // Still optional/nullable rather than a plain string: the backend
  // serializes with exclude_unset, so `"project_desc" in item` stays the
  // honest test for "did the backend even attempt to set this", separate
  // from whether it happens to be empty.
  project_desc?: string | null;
  // This person's own account of what they did -- EmployeeProject.
  // contribution, not the project's own shared description above. Same
  // visibility rule as project_desc: readable by anyone who can see
  // project_history, writable only by hr/work (see app/proposals.py's
  // accept()/edit(), the only two paths that ever set it).
  contribution?: string | null;
}

export interface TrainingStatusItem {
  course_code: string;
  course_name: string;
  // The two-value derivation. The underlying four-value status
  // (not_started / in_progress / failed / completed) never leaves the
  // backend — don't add it here expecting it to arrive.
  display_status: "completed" | "not_completed";
  display_label: string;
  expected: boolean;
  attempted_month?: string | null;
  completed_month?: string | null;
  source: string;
}

export interface NotificationOut {
  id: number;
  kind:
    | "employee_course_reminder"
    | "manager_course_report"
    | "birthday_reminder"
    | "work_anniversary_reminder"
    | "hr_review_reminder";
  subject_person: PersonRef;
  course_name: string;
  display_status: string;
  body: string;
  levels_up: number;
  created_at: string;
}

// Wire shape for PATCH /employees/{id} — mirrors app/schemas.py's
// UpdateEmployeeRequest. Every field optional, and the client only ever
// sends the keys that actually changed: an omitted key means "don't touch",
// an explicit null means "clear it" — the same PATCH-with-partial-dict
// contract app/writes.py's update_employee implements server-side. This
// interface exists so a typo'd field name is a compile error rather than a
// silently-ignored key the backend's extra="forbid" would 422 on at runtime.
export interface UpdateEmployeeChanges {
  full_name?: string;
  preferred_name?: string | null;
  job_title?: string;
  work_email?: string;
  work_phone?: string | null;
  salary?: string | null;
  salary_currency?: string | null;
  date_of_birth?: string | null;
  hire_date?: string;
  cost_centre?: string | null;
  employment_type?: "fte" | "contractor" | "intern";
  linkedin_profile?: string | null;
  // "restricted" hides the profile from everyone but HR (see
  // app/permissions.py's is_record_visible) -- the enforcement is
  // unconditional and pre-existing; this is the write side of it.
  availability_status?: "available" | "away" | "restricted";
  // Reassigning a direct report to a new manager -- the prerequisite
  // app.writes.deactivate_employee's block-until-reassigned rule requires
  // before their old manager can be deactivated.
  manager_id?: string | null;
}

export interface PersonDetail {
  id: string;
  full_name: string;
  preferred_name?: string;
  name_pronunciation?: string;
  job_title?: string;
  org_unit?: string;
  work_email?: string;
  work_phone?: string;
  slack_handle?: string;
  effective_timezone?: string;
  employment_type?: string;
  photo_url?: string;
  office?: OfficeOut;
  manager?: PersonRef;
  delegate?: PersonRef;
  availability_status?: string;
  away_until_month?: string;
  tenure_band?: string;
  bio?: string;
  skills?: SkillOut[];
  languages?: SkillOut[];
  project_history?: ProjectHistoryItem[];
  training_status?: TrainingStatusItem[];
  hire_date?: string;
  cost_centre?: string;
  personal_mobile?: string;
  // HR or the person themselves — never the manager. `salary` is a string,
  // not a number: the backend sends the exact decimal so it can't be mangled
  // by a JSON float on the way here.
  salary?: string;
  salary_currency?: string;
  date_of_birth?: string;
  linkedin_profile?: string;
}

export interface OrgChainNode {
  id: string;
  full_name: string;
  job_title: string;
  org_unit: string;
  depth: number;
  availability_status: string;
  delegate?: PersonRef;
  has_reports: boolean;
}

// Mirrors app/unified_search.py's response shape exactly (GET /search).
// The frontend never classifies a query itself — `mode` is the backend's
// deterministic decision, `overview` is only ever present when
// mode === "assisted". `note`, conversely, is direct-mode-only: plain text
// for a case like the skill-miss broadening fallback, which has something
// to explain but made no model call, so it must never be rendered as an
// AIOverview (no Sparkles/"AI Overview" framing, no reasoning trace).
export interface TraceStep {
  tool: string;
  reason: string;
  args: Record<string, unknown>;
  latency_ms: number;
}

export interface AIOverview {
  answer: string;
  citations: PersonRef[];
  trace: TraceStep[];
}

// Pure Python, deterministic, computed alongside an answer (app.
// assistant_context) -- never the model's own idea, never a second model
// call. `surface` is which assistant's OWN response this is attached to
// ("search" here and on AskResponse; "prd" on a PRD-surface AskResponse),
// informed by the OTHER surface's facts.
export interface FollowUpSuggestion {
  surface: "search" | "prd";
  kind: "requirements_gap" | "unfilled_skill";
  label: string;
  project_name?: string | null;
  skill?: string | null;
  minimum_level?: string | null;
}

export interface UnifiedSearchResponse {
  mode: "direct" | "assisted";
  results: PersonSummary[];
  overview?: AIOverview;
  note?: string;
  // Set only on an assisted-mode answer (direct mode makes no model call,
  // so it never opens/continues a conversation) -- the caller's "search"
  // surface conversation, for GET /conversations/search rehydration.
  conversation_id?: number | null;
  // Present (a real suggestion or null) only in assisted mode -- direct
  // mode has no answer prose to attach one alongside.
  suggestion?: FollowUpSuggestion | null;
}

export type Role = "employee" | "manager" | "hr" | "it";

// Which lens the directory is read through. The SERVER decides: anything
// other than hr is answered in employee mode whatever this says (see
// resolve_view_mode in app/permissions.py), so sending it is a request,
// never a grant.
export type ViewMode = "employee" | "work";

// Roles allowed to switch modes. Mirrors WORK_MODE_ROLES in
// app/permissions.py. Kept in sync by hand, but harmless if it drifts:
// showing the toggle to a role the server pins just makes it a no-op,
// never an escalation.
export const WORK_MODE_ROLES: Role[] = ["hr"];

export interface Identity {
  role: Role;
  id: string;
  name: string;
}

// Staffing Continuity Intelligence (app/continuity.py) — HR-only. Mirrors
// app/schemas.py's continuity response types. Never rendered for a
// non-"hr" identity — see App.tsx's tab gating, the entire
// non-HR-invisibility guarantee on this side of the wire.

export interface DeliveryDependency {
  type: "skill" | "project_role";
  name: string;
  project_id: number;
  employee: PersonRef;
  project_backup_count: number;
  org_backup_count: number;
  // Which pool this dependency's redundancy comes from -- orthogonal to
  // the containing engagement's exposure severity, which can land on
  // "low" for both "project" and "org". "project": someone is already on
  // this engagement. "org": nobody here today, org_backup_count > 0
  // elsewhere. "none": neither.
  redundancy_source: "project" | "org" | "none";
  // "declared": a real recorded fact -- either a required-skill entry
  // (GET/PUT /projects/{id}/required-skills) this person meets, or the
  // project_role dependency (always a recorded fact). "inferred": no
  // required-skill list exists for this project, so this is a heuristic
  // -- any Working/Expert skill the person happens to hold while staffed
  // here, whether or not the engagement actually needs it.
  source: "declared" | "inferred";
}

export interface BackupCandidate {
  id: string;
  full_name: string;
  matching_evidence: string;
}

export interface EngagementExposure {
  project_id: number;
  project_name: string;
  exposure: "none" | "low" | "medium" | "high";
  rule_version: number;
  reasons: string[];
  intersecting_review_count: number;
  days_until_hr_review: number | null;
  days_of_assignment_remaining_after_review: number | null;
  dependencies: DeliveryDependency[];
  backups: Record<string, BackupCandidate[]>;
}

export interface ContinuityOverview {
  rule_version: number;
  window_days: number;
  by_severity: Record<string, number>;
  engagements: EngagementExposure[];
}

export interface AuthorizationRecordOut {
  id: number;
  authorization_type: string;
  effective_from: string;
  effective_until: string | null;
  next_hr_review_date: string | null;
  verification_status: string;
  is_current: boolean;
  verified_at: string | null;
  // Silences the upcoming-review reminder sweep for this due date only —
  // does not mean the review itself is done. See POST
  // /continuity/review-queue/{id}/acknowledge.
  hr_review_acknowledged_at: string | null;
  hr_review_acknowledged_by: string | null;
}

export interface EmployeeContinuityDetail {
  employee: PersonRef;
  current_record: AuthorizationRecordOut | null;
  history: AuthorizationRecordOut[];
  engagements: EngagementExposure[];
}

// GET /continuity/review-queue — the proactive "who is nearing a review
// date" list, independent of engagement intersection. engagements_affected
// can legitimately be 0 (a review with zero delivery consequence is still
// something HR needs to track and verify).
export interface HrReviewQueueItem {
  employee: PersonRef;
  current_record: AuthorizationRecordOut;
  days_until_hr_review: number;
  engagements_affected: number;
  highest_exposure: "none" | "low" | "medium" | "high";
}

// AI-assisted doc upload for HR (app/doc_extraction.py, app/proposals.py) —
// HR-only, work mode only, same non-visibility guarantee App.tsx's tab
// gating gives Continuity: for any other role/mode this page never renders
// and its calls never fire (the backend 403s them regardless).

export interface UploadDocResult {
  doc_id: number;
  filename: string;
  characters_extracted: number;
  doc_type: "project_doc" | "resume";
  people_mentioned: number;
  proposed_changes: number;
  status: string;
}

export interface DocSubjectCandidate {
  employee_id: string;
  full_name: string;
  confidence: number;
  // e.g. "email_match", "name_exact", "name_fuzzy+department_match" — see
  // app/doc_extraction.py's rank_candidates for exactly which strings this
  // can be; rendered as-is, humanised at the display layer.
  match_reason: string;
  // Who this person actually is, so two same-named candidates are
  // distinguishable — full_name and confidence describe what the DOCUMENT
  // said and are identical for both of them. Filled by app/proposals.py's
  // _candidate_details at display time. Optional because an employee row
  // deleted since extraction yields none of it.
  job_title?: string;
  org_unit?: string | null;
  office?: string | null;
  is_active?: boolean;
}

export interface DocSubjectMatchOut {
  id: number;
  source_doc_id: number;
  extracted_name: string;
  extracted_signals: Record<string, string>;
  candidates: DocSubjectCandidate[];
  resolution_status: "unresolved" | "resolved" | "new_hire_candidate";
  resolved_employee_id: string | null;
  resolved_by: string | null;
  resolved_at: string | null;
  proposed_change_count: number;
}

// proposed_value/original_value are deliberately untyped objects, not a
// discriminated union keyed on change_type — the backend treats them the
// same way (a bare JSON blob whose keys depend on change_type), and /edit's
// wire contract is "send back the same keys". Rendered generically as
// key:value pairs rather than three hardcoded per-type layouts.
export interface ProposedChangeOut {
  id: number;
  change_type: "skill" | "contribution" | "project_entry";
  status: "pending" | "accepted" | "edited" | "rejected";
  confidence: number;
  proposed_value: Record<string, unknown>;
  original_value: Record<string, unknown> | null;
  source_doc_id: number;
  subject_match_id: number | null;
  reviewed_by: string | null;
  reviewed_at: string | null;
}

export interface ProposedChangeGroup {
  employee_id: string;
  employee_name: string | null;
  changes: ProposedChangeOut[];
}

// GET /uploaded_docs — one row per document ever uploaded. pending_count and
// unresolved_subject_count are live, computed server-side, so the review
// screen can tell "still awaiting a decision" apart from "finalized"
// (content_scrubbed_at set) without re-deriving it from every subject/change
// row itself.
export interface UploadedDocSummary {
  id: number;
  filename: string;
  uploaded_by: string;
  uploaded_at: string;
  content_scrubbed_at: string | null;
  pending_count: number;
  unresolved_subject_count: number;
}

export interface BulkResultRow {
  id: number;
  ok: boolean;
  status?: string;
  error?: string;
}

// Community Graph (app/community_links.py) — a private per-employee "who to
// contact for what" list. GET /community_links returns only the caller's
// own graph, whatever their role — there is no id parameter anywhere in
// this section that could ask for someone else's.

export interface CommunityLinkOut {
  id: number;
  owner_employee_id: string;
  contact_employee_id: string;
  role_label: string;
  reason: string | null;
  source: "official" | "personal";
  office_id: number | null;
  department_id: number | null;
  is_mentor_link: boolean;
  created_at: string;

  // One of app/community_roles.py's CANONICAL_ROLES for a resolved role;
  // null for a personal link, whose label is whatever the owner typed.
  // frontend/src/community.ts turns this into a caption and an icon.
  role_key: string | null;
  contact_office_name: string | null;
  contact_office_city: string | null;
  // Whole kilometres to the contact's office, and whether the role was
  // answered from another location because the owner's own office has
  // nobody in it. Both empty for roles that don't widen geographically
  // (mentor, technical expert, project contact) and for personal links.
  distance_km: number | null;
  is_remote_fallback: boolean;
}

// HR's review queue for office/role -> candidate mappings bootstrapped from
// existing office/job-title data (GET /suggested_official_links, HR-only —
// see App.tsx's tab gating, same non-visibility guarantee Continuity and
// Review already carry).
export interface SuggestedOfficialLinkOut {
  id: number;
  office_id: number;
  role_label: string;
  candidate_employee_id: string;
  status: "pending" | "confirmed" | "rejected";
  created_at: string;
  reviewed_by: string | null;
  reviewed_at: string | null;
}

// Follow-up chat (POST /ask — app.schemas.HistoryTurn/AskRequest). A turn
// held client-side for the length of this browser session, never persisted
// server-side. tool_call/arguments are a PLAN, not a result — the server
// re-executes them fresh, through the ordinary enforce()-gated dispatcher,
// on every new turn (see app/tool_calling.py's _history_messages). Only
// assistant_text is ever carried as-given, and only for a turn that had no
// tool call (a clarifying question, an out-of-scope reply) — connective
// language with no factual claim about a person.
export interface AskHistoryTurn {
  message: string;
  tool_call?: string | null;
  arguments?: Record<string, unknown> | null;
  assistant_text?: string | null;
}

// One step of a multi-step chain, PLAN + real measured timing (tool,
// arguments, latency_ms) — never a step's result. See app/tool_calling.py
// execute_chain's `steps` key.
export interface AskStep {
  tool: string;
  arguments: Record<string, unknown>;
  latency_ms: number;
}

// null when a chain finished on its own within budget (or wasn't a chain
// at all); otherwise which axis of the plan class's budget ended it —
// "steps" | "records" | "wall_clock" (app/chain_budgets.py). The note
// this implies is already folded into `message` server-side, so no UI
// needs to read this field to show the user it's incomplete — it's here
// for anything that wants to distinguish the reason programmatically.
export type ChainTruncationReason = "steps" | "records" | "wall_clock" | null;

export interface AskResponse {
  message: string | null;
  tool_call: string | null;
  arguments: Record<string, unknown> | null;
  result: unknown;
  steps?: AskStep[];
  truncated?: ChainTruncationReason;
  // The caller's "search" (POST /ask) or "prd" (POST /prd/ask) surface
  // conversation -- pass it back on the next call on this thread instead
  // of building a history array client-side; see GET /conversations/{surface}.
  conversation_id?: number | null;
  // Present only when a real answer was produced (a tool call happened,
  // not an out-of-scope reply) -- a real suggestion, or null.
  suggestion?: FollowUpSuggestion | null;
}

// GET /conversations/{surface} -- how a page reload rehydrates. `turns` are
// PLANS only (see AskHistoryTurn above), never a stored result or phrased
// answer, so a turn with a tool_call has no answer text or citations to
// replay -- only its question and which tool it resolved to.
export interface ConversationOut {
  conversation_id: number | null;
  turns: AskHistoryTurn[];
}

// --- PRD chatbot: requirements (app/project_requirements.py, app/schemas.py)
//
// One row of the PRD page's project picker (GET /projects).
export interface ProjectListItem {
  id: number;
  name: string;
  type: string;
  is_client_engagement: boolean;
  has_requirements: boolean;
}

export interface ProjectSkillRequirementIn {
  skill: string;
  minimum_level: "Learning" | "Working" | "Expert";
  // Defaults false server-side. True only for a row already shown to a
  // human as "not yet in the system" and explicitly kept (PRDsPage's "new
  // skills" section) -- app/project_skills.py's set_required_skills still
  // rejects an unknown name outright when this is absent/false, same as a
  // hand-typed entry with a typo always has.
  create_if_missing?: boolean;
}

export interface ProjectSkillRequirementOut {
  skill: string;
  minimum_level: string;
}

export interface RequirementNoteIn {
  note: string;
  source_doc_id?: number | null;
}

export interface RequirementNoteOut {
  note: string;
  source_doc_id?: number | null;
}

// What get_project_requirements (the PRD assistant's own tool) answers
// with for one resolved project -- skills and notes together.
export interface ProjectRequirementsOut {
  project_name: string;
  description?: string | null;
  skills: ProjectSkillRequirementOut[];
  notes: RequirementNoteOut[];
}

// One row of list_project_requirements_summary -- the PRD assistant's
// other tool, for "what have we captured so far" questions. Counts only.
export interface ProjectRequirementsSummaryItem {
  project_name: string;
  skill_count: number;
  note_count: number;
}

// POST /projects/{id}/prd's response -- a PREVIEW only, nothing saved yet.
// `confidence` is extraction-quality metadata for the review UI, not
// persisted anywhere once confirmed.
export interface PrdSkillProposal {
  skill: string;
  minimum_level: string;
  confidence: number;
}

export interface PrdNoteProposal {
  note: string;
  confidence: number;
}

export interface UploadPrdResult {
  doc_id: number;
  filename: string;
  skills: PrdSkillProposal[];
  // Same shape as `skills` -- proposed, but the name doesn't resolve
  // against the skill catalog yet. Confirming one of these means PUT
  // .../required-skills will CREATE that Skill row (create_if_missing),
  // not just attach an existing one -- kept separate from `skills` so the
  // frontend can show HR which is which before they confirm either way.
  new_skills: PrdSkillProposal[];
  notes: PrdNoteProposal[];
}

// --- Dashboards (app/analytics.py) ----------------------------------------
//
// HR's and a manager's payloads are structurally identical; what differs is
// the DashboardScope each was computed over. The narrowing happens
// server-side (app/analytics.py's resolve_scope) and is never something this
// client reproduces -- it only decides what to OFFER, same division of
// responsibility as WORK_MODE_ROLES above.

export interface DashboardScope {
  kind: "org" | "org_unit" | "team";
  label: string;
  headcount: number;
  org_unit_id: number | null;
  org_unit: string | null;
  manager_id: string | null;
  // True when the caller asked for one scope and the server gave another --
  // a manager sending an org_unit_id. The header says so rather than leaving
  // a selector pointing at data it didn't produce.
  substituted: boolean;
}

export interface OrgUnitOption {
  id: number;
  name: string;
  unit_type: string;
  parent_id: number | null;
  // SUBTREE headcount: what picking this option actually scopes to.
  headcount: number;
}

export type TrainingBucket = "completed" | "overdue" | "due_soon" | "outstanding";

// The four buckets are mutually exclusive and sum to `expected`, so they
// render as one donut. `incomplete` is the rollup of the three non-completed
// ones and deliberately overlaps them -- never add it to the chart.
export interface TrainingBuckets {
  expected: number;
  completed: number;
  incomplete: number;
  overdue: number;
  due_soon: number;
  outstanding: number;
  compliance_pct: number;
}

export interface TrainingBreakdown {
  key: string;
  label: string;
  buckets: TrainingBuckets;
  employee_count: number;
}

export interface TrainingAnalytics {
  scope: DashboardScope;
  buckets: TrainingBuckets;
  employee_count: number;
  no_record_count: number;
  no_deadline_count: number;
  due_soon_days: number;
  by_course: TrainingBreakdown[];
  by_unit: TrainingBreakdown[];
  courses: TrainingBreakdown[];
}

export interface TrainingPersonRow {
  employee_id: string;
  full_name: string;
  job_title: string;
  org_unit: string;
  course_code: string;
  course_name: string;
  display_status: "completed" | "not_completed";
  bucket: TrainingBucket;
  due_on: string | null;
  days_overdue: number | null;
  has_record: boolean;
}

export interface TrainingRoster {
  rows: TrainingPersonRow[];
  total: number;
  truncated: boolean;
}

export interface ReminderResult {
  requested: number;
  eligible: number;
  sent: number;
  recipients_notified: number;
  out_of_scope: number;
  skipped: number;
  detail: string;
}

export type SkillVerdict = "understaffed" | "healthy" | "overrepresented" | "unused";

export interface SkillSupplyDemand {
  skill_id: number;
  skill: string;
  category: string;
  expert_count: number;
  working_count: number;
  learning_count: number;
  capable_count: number;
  holder_count: number;
  demand_project_count: number;
  // "declared" = a project recorded this as a requirement; "inferred" = read
  // off what assigned people happen to know, which overcounts. Always shown.
  demand_basis: "declared" | "inferred" | "none";
  declared_project_count: number;
  supply_per_project: number | null;
  verdict: SkillVerdict;
  single_point_of_failure: boolean;
  coverage_pct: number;
  maturity_pct: number;
  maturity_label: string;
}

export interface SkillHolder {
  id: string;
  full_name: string;
  job_title: string;
  org_unit: string;
  level: "Expert" | "Working" | "Learning";
}

export interface SkillProjectUse {
  project_id: number;
  project_name: string;
  basis: "declared" | "inferred";
  member_count: number;
  capable_member_count: number;
}

export interface SkillDetail {
  scope: DashboardScope;
  skill_id: number;
  skill: string;
  category: string;
  expert_count: number;
  working_count: number;
  learning_count: number;
  capable_count: number;
  holder_count: number;
  coverage_pct: number;
  maturity_pct: number;
  maturity_label: string;
  demand_project_count: number;
  supply_per_project: number | null;
  verdict: SkillVerdict;
  risk: "high" | "medium" | "low";
  risk_reason: string;
  holders: SkillHolder[];
  holders_truncated: boolean;
  projects: SkillProjectUse[];
}

export interface ProjectCoverage {
  project_id: number;
  project_name: string;
  project_type: string;
  is_client_engagement: boolean;
  member_count: number;
  in_scope_member_count: number;
  // False means nothing was declared and no verdict is offered --
  // coverage_pct is null and risk is "unknown".
  requirements_recorded: boolean;
  required_skill_count: number;
  covered_skill_count: number;
  coverage_pct: number | null;
  gap_skills: string[];
  single_cover_skills: string[];
  risk: "high" | "medium" | "low" | "unknown";
}

export interface WorkforceInsight {
  kind:
    | "skill_shortage" | "skill_concentration" | "training_compliance"
    | "project_staffing_gap" | "profile_coverage" | "bench_capacity";
  severity: "high" | "medium" | "low";
  title: string;
  detail: string;
  evidence: string[];
  skill_ids: number[];
  project_ids: number[];
  recommendation: string;
}

// The dashboard's opening paragraph. `source` is not decoration: "model"
// means a language model ordered the already-computed findings into prose
// and every numeral it wrote was checked back against them; "derived" means
// a format string assembled the same facts, which is what runs with no
// model configured, on a failed call, and on a failed check. Rendered as
// provenance so a screenshot can never pass one off as the other.
export interface InsightNarrative {
  text: string;
  source: "model" | "derived";
}

export interface InsightReport {
  summary: InsightNarrative;
  insights: WorkforceInsight[];
}

export interface DashboardOverview {
  scope: DashboardScope;
  headcount: number;
  department_count: number;
  division_count: number;
  team_count: number;
  manager_count: number | null;
  active_project_count: number;
  client_engagement_count: number;
  skill_count: number;
  expert_count: number;
  people_with_skills: number;
  skill_profile_coverage_pct: number;
  avg_skills_per_person: number;
  understaffed_skill_count: number;
  healthy_skill_count: number;
  overrepresented_skill_count: number;
  unused_skill_count: number;
  single_point_skill_count: number;
  training: TrainingBuckets;
  training_employee_count: number;
  due_soon_days: number;
}

// --- Skill bridges (app/skill_routes.py) ----------------------------------

export interface SkillTarget {
  skill_id: number;
  skill: string;
  category: string;
  capable_count: number;
}

export interface SkillRouteHop {
  person: { id: string; full_name: string };
  job_title: string;
  // WHY this step exists. A path with unlabelled edges tells you to go talk
  // to a stranger; "you are both on Payroll Annual Planning" tells you how
  // to open.
  via_kind: "project" | "team" | "past_project" | "skill";
  via: string;
}

export interface SkillRoute {
  target: { id: string; full_name: string };
  job_title: string;
  level: "Expert" | "Working";
  hops: SkillRouteHop[];
}

export interface SkillRouteResult {
  // null when the name matched no skill -- reported as unresolved rather
  // than as zero routes, which would read as "nobody has it".
  skill: SkillTarget | null;
  requested: string;
  from_person: { id: string; full_name: string };
  already_capable: boolean;
  routes: SkillRoute[];
  unreachable_holder_count: number;
}

export interface SuggestedSkill {
  skill_id: number;
  skill: string;
  capable_count: number;
  reason: string;
}

// --- Workforce Intelligence reports (app/workforce_reports.py) ------------

export type AnalysisType = "skill_gap" | "skill_scarcity" | "training" | "project_coverage";

// One checkable fact behind a finding. The ids are the click-through: they
// open the SAME skill / training / project surfaces the dashboard already
// has, rather than a report-only rendering of the same data.
export interface ReportEvidence {
  kind: "skill" | "project" | "training" | "department";
  label: string;
  skill_id: number | null;
  project_id: number | null;
  course_code: string | null;
  org_unit_id: number | null;
}

export interface ReportFinding {
  title: string;
  detail: string;
  // Assigned by the deterministic analysis, never by the model -- how bad
  // something is follows from the counts.
  severity: "high" | "medium" | "low" | "info";
  evidence: ReportEvidence[];
}

export interface ReportSection {
  heading: string;
  findings: ReportFinding[];
}

export interface WorkforceReport {
  title: string;
  query: string;
  scope: DashboardScope;
  analyses: AnalysisType[];
  unsupported: string[];
  executive_summary: string;
  // "model" = written by a model over the findings and numeral-checked
  // against them; "derived" = assembled deterministically.
  narrative_source: "model" | "derived";
  strengths: ReportSection;
  skill_gaps: ReportSection;
  risks: ReportSection;
  training_insights: ReportSection;
  project_insights: ReportSection;
  recommendations: ReportSection;
  evidence: ReportEvidence[];
}

// --- AI Team Builder (app/team_builder.py) --------------------------------
//
// Everything numeric here is computed server-side from database rows. The
// model contributes the role/skill breakdown and (optionally) `narrative`,
// whose numerals are checked against the computed facts before it is kept.

export interface CandidateSkill {
  skill: string;
  level: "Expert" | "Working" | "Learning";
  // false for skills the person brings that the role did not ask for --
  // shown as context, never part of match_pct.
  required: boolean;
}

export interface CandidateMatch {
  employee_id: string;
  full_name: string;
  job_title: string;
  org_unit: string;
  // Displayed, never ranked on: the column is 99% one value, see the
  // module docstring in app/team_builder.py.
  availability_status: string;
  match_pct: number;
  matched_skills: CandidateSkill[];
  missing_skills: string[];
  relevant_projects: string[];
  explanation: string[];
}

export interface ProposedRole {
  role: string;
  required_skills: string[];
  // null when nobody in the authorized pool holds any of the role's skills.
  candidate: CandidateMatch | null;
  alternatives: CandidateMatch[];
}

export interface TeamCoverageSkill {
  skill: string;
  best_level: "Expert" | "Working" | "Learning" | null;
  holder_count: number;
  holders: string[];
}

export interface TeamConcentrationRisk {
  skill: string;
  employee_id: string;
  full_name: string;
  share_pct: number;
  holder_count: number;
}

export interface TeamCoverage {
  coverage_pct: number;
  skills: TeamCoverageSkill[];
  // A skill held only at Learning counts toward coverage_pct at partial
  // weight but is NOT listed as covered -- "we have some exposure" and
  // "somebody here can do this" are different statements.
  covered: string[];
  missing: string[];
  level_counts: Record<string, number>;
  risks: TeamConcentrationRisk[];
}

export interface TeamConstraintsOut {
  prefer_expert: boolean;
  minimize_concentration: boolean;
  max_per_department: number | null;
  prefer_experience_with: string[];
  applied: boolean;
}

export interface TeamRoleInput {
  role: string;
  required_skills: string[];
}

/** The plan echoed back on a rebuild, so Replace re-staffs the SAME team
 *  rather than re-planning it. Carries roles and skills only — it cannot
 *  name a person or a scope, and every skill is re-resolved server-side. */
export interface TeamPlanInput {
  project_type: string;
  roles: TeamRoleInput[];
}

export interface TeamProposal {
  scope: DashboardScope;
  project_type: string;
  roles: ProposedRole[];
  coverage: TeamCoverage;
  constraints: TeamConstraintsOut;
  unrecognised_skills: string[];
  plan_source: "model" | "derived";
  narrative: string;
  narrative_source: "model" | "derived";
  candidate_pool_size: number;
}

// --- Find the Right Team (app/team_finder.py) -----------------------------
//
// Recommends an EXISTING org unit. Every count is computed server-side over
// employees the caller is permitted to discover, so a headcount here cannot
// disclose somebody they cannot see.

export interface TeamMatchSkill {
  skill: string;
  expert: number;
  working: number;
  learning: number;
  total: number;
}

export interface TeamManagerRef {
  employee_id: string;
  full_name: string;
  job_title: string;
  work_email: string;
}

export interface TeamRecommendation {
  org_unit_id: number;
  name: string;
  unit_type: string;
  match_pct: number;
  headcount: number;
  relevant_people: number;
  skills: TeamMatchSkill[];
  projects: string[];
  manager: TeamManagerRef | null;
  why: string;
}

export interface TeamRecommendationResult {
  query: string;
  topic: string;
  skills: string[];
  teams: TeamRecommendation[];
  unrecognised_skills: string[];
  need_source: "model" | "derived";
  // "team"/"department" when the question said which; results of that type
  // sort first, the other type still appears below.
  preferred_unit_type: string | null;
}

/** What the signed-in caller may actually do (GET /me/capabilities).
 *
 *  Server-computed, because neither thing the client could check is
 *  reliable: the role claim alone lets a "manager" with no reports through,
 *  and PersonSummary.has_reports is absent in employee view mode — the only
 *  mode a manager ever gets. Advisory; every endpoint still enforces. */
export interface MeCapabilities {
  can_build_team: boolean;
  can_find_team: boolean;
}
