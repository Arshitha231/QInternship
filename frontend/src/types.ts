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
  hire_date?: string;
  cost_centre?: string;
  personal_mobile?: string;
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

export type Role = "employee" | "manager" | "hr";

export interface Identity {
  role: Role;
  id: string;
  name: string;
}
