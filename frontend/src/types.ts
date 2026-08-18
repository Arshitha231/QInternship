// Mirrors app/schemas.py. Fields are optional throughout because the API
// serializes with response_model_exclude_unset=True — a field the caller
// can't see is genuinely absent from the JSON, not null.

export interface OfficeOut {
  id: number;
  name: string;
  city: string;
  country: string;
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
}

export interface SkillOut {
  name: string;
  category: string;
  level: string;
  source: string;
}

export interface ProjectHistoryItem {
  project_name: string;
  project_type: string;
  role: string;
  start_month: string;
  end_month: string | null;
  current: boolean;
  // Visible to every role/view_mode that can see project_history at all --
  // EDITABLE gates who may WRITE this (it/work only), not who may read it.
  // Still optional/nullable rather than a plain string: the backend
  // serializes with exclude_unset, so `"project_desc" in item` stays the
  // honest test for "did the backend even attempt to set this", separate
  // from whether it happens to be empty.
  project_desc?: string | null;
  // This person's own account of what they did -- EmployeeProject.
  // contribution, not the project's own shared description above. Same
  // visibility rule as project_desc: readable by anyone who can see
  // project_history, writable only by it/work (see app/proposals.py's
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
    | "work_anniversary_reminder";
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
// mode === "assisted".
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

export interface UnifiedSearchResponse {
  mode: "direct" | "assisted";
  results: PersonSummary[];
  overview?: AIOverview;
}

export type Role = "employee" | "manager" | "hr" | "it";

// Which lens the directory is read through. The SERVER decides: anything
// other than hr/it is answered in employee mode whatever this says (see
// resolve_view_mode in app/permissions.py), so sending it is a request,
// never a grant.
export type ViewMode = "employee" | "work";

// Roles allowed to switch modes. Mirrors WORK_MODE_ROLES in
// app/permissions.py. Kept in sync by hand, but harmless if it drifts:
// showing the toggle to a role the server pins just makes it a no-op,
// never an escalation.
export const WORK_MODE_ROLES: Role[] = ["hr", "it"];

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

// AI-assisted doc upload for IT (app/doc_extraction.py, app/proposals.py) —
// IT-only, work mode only, same non-visibility guarantee App.tsx's tab
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
  // Marks the subset of official links whose expiration is computed
  // server-side at read time — never something the frontend calculates.
  is_mentor_link: boolean;
  created_at: string;
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
