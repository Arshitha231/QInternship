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
  // Work mode, hr/it only. Absent (not null) for anyone else -- the backend
  // serializes with exclude_unset, so `"project_desc" in item` is the honest
  // test for "am I allowed to see this", and undefined means no.
  project_desc?: string | null;
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

export interface PersonDetail {
  id: string;
  full_name: string;
  preferred_name?: string;
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
