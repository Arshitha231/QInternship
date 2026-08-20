import type {
  BulkResultRow, CommunityLinkOut, ContinuityOverview, DocSubjectMatchOut, EmployeeContinuityDetail,
  EngagementExposure, HrReviewQueueItem, Identity, NotificationOut, OfficeOut, OrgChainNode, OrgUnitOut,
  PersonDetail, PersonSummary, ProposedChangeGroup, SuggestedOfficialLinkOut, UnifiedSearchResponse,
  UpdateEmployeeChanges, UploadDocResult, UploadedDocSummary, ViewMode,
} from "./types";

// Defaults to the local backend for normal dev. Override with
// VITE_API_BASE (see package.json's "dev:live" script) to point this same
// frontend at the deployed Azure backend instead -- e.g. to view real
// deployed data without running uvicorn locally.
export const API_BASE = import.meta.env.VITE_API_BASE ?? "http://127.0.0.1:8000";

export const UNAUTHORIZED_EVENT = "orghub:unauthorized";

export class ApiError extends Error {
  status: number;
  // The raw, still-structured response body's "detail" key -- most routes
  // send a plain string there and `message` alone is enough, but a few (see
  // deactivateEmployee's 409) send a structured object so the caller can
  // act on it (e.g. render the list of direct reports blocking it) instead
  // of re-parsing a stringified message.
  detail?: unknown;
  constructor(status: number, message: string, detail?: unknown) {
    super(message);
    this.status = status;
    this.detail = detail;
  }
}

function headers(identity: Identity): HeadersInit {
  return {
    "X-Dev-Role": identity.role,
    "X-Dev-User-Id": identity.id,
    "X-Dev-Name": identity.name,
  };
}

async function request<T>(path: string, identity: Identity, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { ...headers(identity), ...(init?.headers ?? {}) },
  });
  if (!res.ok) {
    // A 401 means the identity these headers carry is no longer accepted --
    // the backend was restarted into entra mode, say. Broadcast it so App
    // can drop the session and show the login form, rather than leaving
    // every panel showing its own error. Dispatched, not imported: session.ts
    // imports API_BASE from this module, and calling into it here would make
    // the pair circular.
    if (res.status === 401) window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
    throw new ApiError(res.status, `${res.status} ${res.statusText}`);
  }
  return res.json() as Promise<T>;
}

// --- Sign-in (POST /auth/login). The only call in this file that doesn't
// take an Identity -- it's what produces one. 404s if the backend is
// running real auth, where sign-in is Entra's job; see app/demo_auth.py.

export async function login(email: string, password: string): Promise<Identity> {
  const res = await fetch(`${API_BASE}/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    // Pass the server's own message through rather than mapping status codes
    // here: a wrong password, an unknown email and a deactivated employee all
    // come back as the same 401, and that sameness is deliberate (see
    // app/demo_auth.py's DemoLoginDenied).
    const detail = typeof body?.detail === "string" ? body.detail : `${res.status} ${res.statusText}`;
    throw new ApiError(res.status, detail, body?.detail);
  }
  // The response is an AuthenticatedUser; Identity is the subset the
  // request headers actually carry.
  return { id: body.id, role: body.role, name: body.name };
}

export interface SearchFilters {
  name?: string;
  query?: string;
  skill?: string;
  level?: string;
  org_unit?: string;
  office?: string;
  language?: string;
  available?: boolean;
}

// view_mode is sent on every directory/profile read. Passing it for an
// employee/manager identity is harmless and deliberate -- the server pins
// those roles to employee mode regardless, so the frontend never has to
// decide who is allowed what. It only decides what to OFFER (see
// WORK_MODE_ROLES); the answer always comes from the backend.
export function findPeople(
  identity: Identity, filters: SearchFilters, viewMode: ViewMode, signal?: AbortSignal,
): Promise<PersonSummary[]> {
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(filters)) {
    if (v !== undefined && v !== "" && v !== null) params.set(k, String(v));
  }
  params.set("view_mode", viewMode);
  const qs = params.toString();
  return request<PersonSummary[]>(`/people${qs ? `?${qs}` : ""}`, identity, { signal });
}

export async function getPerson(
  identity: Identity, personId: string, viewMode: ViewMode,
): Promise<PersonDetail | null> {
  try {
    return await request<PersonDetail>(`/people/${personId}?view_mode=${viewMode}`, identity);
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return null;
    throw e;
  }
}

export function updateOwnBio(identity: Identity, bio: string): Promise<PersonDetail> {
  return request<PersonDetail>(`/people/${identity.id}/bio`, identity, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ bio }),
  });
}

export function updateOwnNamePronunciation(identity: Identity, namePronunciation: string): Promise<PersonDetail> {
  return request<PersonDetail>(`/people/${identity.id}/pronunciation`, identity, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name_pronunciation: namePronunciation }),
  });
}

// --- Self-service skills and languages -----------------------------------
// Own profile only (app/main.py's _require_self), whatever role the caller
// holds — the same identity gate updateOwnBio goes through, not the
// role-keyed EDITABLE table the HR/IT writes below use.
//
// A language is a skills row with category="language", so one set of calls
// serves both cards; `category` only decides where a BRAND-NEW name gets
// filed, and a name that already exists on the other side of the
// Skills/Languages split is a 422 rather than being quietly refiled.
//
// All three resolve to the caller's full PersonDetail, so ProfilePage can
// replace `skills` and `languages` from the response instead of refetching
// — the server's answer is where the entry actually landed, which for an
// add is not always the card that submitted it.

export type SkillLevelName = "Learning" | "Working" | "Expert";
export type SkillCategoryName = "technical" | "domain" | "language";

// Bespoke fetch rather than request<T>, same reason requestEmployeeAction
// below has one: these routes send a real explanation in `detail` ("French
// is a language skill, not technical…", "You already have X on your
// profile…") and the generic helper discards it in favour of the status
// line, which is exactly the text a person editing their own profile
// should never be shown.
async function skillRequest(
  identity: Identity, path: string, init: RequestInit,
): Promise<PersonDetail> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { ...headers(identity), ...(init.headers ?? {}) },
  });
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    if (res.status === 401) window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
    // 422 from pydantic is an array of field errors, not a string — fall
    // back to the status line there rather than rendering "[object Object]".
    const detail = typeof body?.detail === "string" ? body.detail : `${res.status} ${res.statusText}`;
    throw new ApiError(res.status, detail, body?.detail);
  }
  return body as PersonDetail;
}

export function addOwnSkill(
  identity: Identity, skill: string, category: SkillCategoryName, level: SkillLevelName,
): Promise<PersonDetail> {
  return skillRequest(identity, `/people/${identity.id}/skills`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ skill, category, level }),
  });
}

export function updateOwnSkill(
  identity: Identity, skill: string, level: SkillLevelName,
): Promise<PersonDetail> {
  return skillRequest(identity, `/people/${identity.id}/skills`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ skill, level }),
  });
}

// Name goes in the query string, not the path: real skill names contain
// slashes ("CI/CD", "Agile/Scrum") and a percent-encoded slash is decoded
// back into a path separator before the server routes it.
export function removeOwnSkill(identity: Identity, skill: string): Promise<PersonDetail> {
  return skillRequest(
    identity, `/people/${identity.id}/skills?skill=${encodeURIComponent(skill)}`, { method: "DELETE" },
  );
}

// HR, work mode, any employee but themselves — see app/writes.py's
// update_employee for the actual enforcement; this call succeeding or not
// is the server's decision, not something checked here. `changes` should
// carry only the fields that actually changed (undefined keys are dropped
// by JSON.stringify automatically, matching the backend's exclude_unset
// contract) — the caller (ProfilePage) is responsible for diffing against
// the loaded profile before calling this, not this function.
export function updateEmployee(
  identity: Identity, personId: string, changes: UpdateEmployeeChanges, viewMode: ViewMode,
): Promise<PersonDetail> {
  return request<PersonDetail>(`/employees/${personId}?view_mode=${viewMode}`, identity, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(changes),
  });
}

// --- HR, work mode: edit anyone's project history except their own -------
// Both calls 403 for any non-"hr" identity, in employee mode, and on the
// caller's OWN record (app/writes.py's _refuse_own_record) — ProfilePage
// only renders the controls when all three already hold, but that is a
// convenience, not the enforcement.

// The subset of an EmployeeProject row this path may write. Dates are ISO
// (YYYY-MM-DD) rather than the month strings the READ side returns:
// ProjectHistoryItem deliberately publishes month precision only, so a
// month picked in the UI becomes the 1st of that month here rather than
// this pretending to round-trip a day it was never told.
export interface ProjectHistoryChanges {
  role?: string;
  contribution?: string | null;
  start_date?: string;
  end_date?: string | null;
}

export interface ProjectHistoryRow {
  employee_id: string;
  project_id: number;
  role: string;
  contribution: string | null;
  start_date: string | null;
  end_date: string | null;
}

// Creates the membership if it doesn't exist, patches it if it does —
// (person, project) identifies the row, so this is safe to repeat. Only
// the keys present are written; an explicit null clears (end_date: null is
// how a project becomes current again), while an omitted key is left
// alone. Same diff-before-calling contract as updateEmployee above.
export function upsertProjectHistory(
  identity: Identity, personId: string, projectId: number,
  changes: ProjectHistoryChanges, viewMode: ViewMode,
): Promise<ProjectHistoryRow> {
  return request<ProjectHistoryRow>(
    `/people/${personId}/projects/${projectId}?view_mode=${viewMode}`, identity, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(changes),
    });
}

// 204, no body — hence the bare fetch rather than request<T>, same shape
// deleteCommunityLink uses.
export async function removeProjectHistory(
  identity: Identity, personId: string, projectId: number, viewMode: ViewMode,
): Promise<void> {
  const res = await fetch(
    `${API_BASE}/people/${personId}/projects/${projectId}?view_mode=${viewMode}`,
    { method: "DELETE", headers: headers(identity) },
  );
  if (!res.ok) {
    throw new ApiError(res.status, `${res.status} ${res.statusText}`);
  }
}

// The plain summary app/main.py's _employee_action_result returns —
// deliberately not PersonDetail, since get_person returns nothing for an
// inactive record (the very state deactivate/reactivate transition
// through), for every caller including HR.
export interface EmployeeActionResult {
  id: string;
  full_name: string;
  job_title: string;
  is_active: boolean;
  availability_status: string;
  deactivated_at: string | null;
}

export interface ActiveDirectReport {
  id: string;
  full_name: string;
}

export interface CreateEmployeeFields {
  full_name: string;
  job_title: string;
  org_unit_id: number;
  work_email: string;
  employment_type: "fte" | "contractor" | "intern";
  preferred_name?: string;
  office_id?: number;
  manager_id?: string;
  work_phone?: string;
  hire_date?: string;
  // Not a field on the employee — becomes an official mentor link in their
  // community graph when the request is approved (app.writes._apply_creation).
  mentor_id?: string;
}

export function listOrgUnits(identity: Identity): Promise<OrgUnitOut[]> {
  return request<OrgUnitOut[]>("/org_units", identity);
}

export function listOffices(identity: Identity): Promise<OfficeOut[]> {
  return request<OfficeOut[]>("/offices", identity);
}

// Stages a request; creates nobody. Returns the pending approval, not an
// employee — adding a person is a two-person action, same as restricting or
// deactivating one (app.writes.request_creation).
export function requestEmployeeCreation(
  identity: Identity, fields: CreateEmployeeFields, viewMode: ViewMode,
): Promise<ActionRequestResult> {
  return request<ActionRequestResult>(`/employees?view_mode=${viewMode}`, identity, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(fields),
  });
}

// Bespoke fetch, not the generic request<T> helper — a blocked deactivation
// (409) sends a structured body ({message, active_direct_reports}), and the
// generic helper's ApiError only ever carries a plain string built from the
// status line, discarding the parsed JSON entirely. The caller (ProfilePage)
// needs the actual list to render an inline reassignment picker, not just
// the fact that it was blocked.
// The shape app/main.py's _action_request_result returns for restrict/
// deactivate/create — a staged request, not an applied change. Nothing here
// takes effect until the resolved approver (approver_id/approver_name) acts
// on it — see approveActionRequest/rejectActionRequest below.
export interface ActionRequestResult {
  request_id: number;
  action_type: "restrict" | "deactivate" | "create";
  status: "pending" | "approved" | "rejected";
  // Null for a pending "create": the person being proposed has no id until
  // the approval creates them. target_name is always populated (read from
  // the request's payload in that case), so render that, never target_id.
  target_id: string | null;
  target_name: string;
  approver_id: string | null;
  approver_name: string | null;
  requested_by: string;
  requested_by_name: string;
  created_at: string;
  resolved_at: string | null;
  rejection_reason: string | null;
}

// Bespoke fetch, not the generic request<T> helper — a blocked request
// (409, active direct reports still assigned) sends a structured body
// ({message, active_direct_reports}), and the generic helper's ApiError
// only ever carries a plain string built from the status line, discarding
// the parsed JSON entirely. The caller (ProfilePage) needs the actual list
// to render an inline reassignment picker, not just the fact it was blocked.
async function requestEmployeeAction(
  identity: Identity, path: string, viewMode: ViewMode,
): Promise<ActionRequestResult> {
  const res = await fetch(`${API_BASE}${path}?view_mode=${viewMode}`, {
    method: "POST",
    headers: headers(identity),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    const detail = body?.detail;
    const message = typeof detail === "string" ? detail : (detail?.message ?? `${res.status} ${res.statusText}`);
    throw new ApiError(res.status, message, detail);
  }
  return res.json() as Promise<ActionRequestResult>;
}

// Stages a restrict request — the profile is NOT restricted by this call.
// Only approveActionRequest, called by the resolved approver, applies it.
export const requestRestriction = (identity: Identity, personId: string, viewMode: ViewMode) =>
  requestEmployeeAction(identity, `/employees/${personId}/restrict`, viewMode);

// Stages a deactivate request — the employee is NOT deactivated by this
// call. Still blocked (409) up front while they manage anyone active.
export const requestDeactivation = (identity: Identity, personId: string, viewMode: ViewMode) =>
  requestEmployeeAction(identity, `/employees/${personId}/deactivate`, viewMode);

export function approveActionRequest(
  identity: Identity, requestId: number, viewMode: ViewMode,
): Promise<ActionRequestResult> {
  return request<ActionRequestResult>(`/employee_action_requests/${requestId}/approve?view_mode=${viewMode}`, identity, {
    method: "POST",
  });
}

export function rejectActionRequest(
  identity: Identity, requestId: number, viewMode: ViewMode, reason?: string,
): Promise<ActionRequestResult> {
  return request<ActionRequestResult>(`/employee_action_requests/${requestId}/reject?view_mode=${viewMode}`, identity, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ reason: reason ?? null }),
  });
}

// No view_mode — identity-scoped, not role-scoped (see
// app.writes.list_my_pending_approvals: whoever the requester's reporting
// chain names as approver sees these, whatever role header they're using).
export function listPendingApprovals(identity: Identity): Promise<{ requests: ActionRequestResult[] }> {
  return request(`/employee_action_requests`, identity);
}

// Narrow by design — see app.writes.DEACTIVATED_FIELDS. Identity and
// placement only; this is the one carve-out that sees is_active=false
// records at all, so it stays as small as it can.
export interface DeactivatedEmployee {
  id: string;
  full_name: string;
  job_title: string;
  org_unit: string | null;
  work_email: string;
  deactivated_at: string | null;
}

// The only call that surfaces deactivated employees. Every other read in
// this app treats them as nonexistent, which is what made reactivate
// unreachable from the UI without knowing an id by heart.
export function listDeactivatedEmployees(
  identity: Identity, viewMode: ViewMode,
): Promise<{ employees: DeactivatedEmployee[] }> {
  return request(`/employees/deactivated?view_mode=${viewMode}`, identity);
}

export function reactivateEmployee(
  identity: Identity, personId: string, viewMode: ViewMode,
): Promise<EmployeeActionResult> {
  return request<EmployeeActionResult>(`/employees/${personId}/reactivate?view_mode=${viewMode}`, identity, {
    method: "POST",
  });
}

// viewMode matters for direction="down": an hr caller previewing the
// ordinary view loses the downward chain, exactly as they lose every other
// privilege there. (A manager keeps their own team chart — they are pinned
// to employee mode permanently and never had a work mode to give up; see
// app.policy.is_previewing_ordinary_view.)
export async function getOrgChart(
  identity: Identity,
  personId: string,
  direction: "up" | "down",
  viewMode: ViewMode,
  depth = 10,
): Promise<OrgChainNode[]> {
  try {
    return await request<OrgChainNode[]>(
      `/people/${personId}/org-chart?direction=${direction}&depth=${depth}&view_mode=${viewMode}`,
      identity);
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return [];
    throw e;
  }
}

// Your own notifications only — the endpoint takes no person id at all, so
// there's deliberately no way to ask for anyone else's.
export function getMyNotifications(identity: Identity, signal?: AbortSignal): Promise<NotificationOut[]> {
  return request<NotificationOut[]>("/me/notifications", identity, { signal });
}

export interface UnifiedSearchFilters {
  q?: string;
  skill?: string;
  level?: string;
  org_unit?: string;
  office?: string;
  language?: string;
  available?: boolean;
}

export function unifiedSearch(
  identity: Identity, filters: UnifiedSearchFilters, viewMode: ViewMode, signal?: AbortSignal,
): Promise<UnifiedSearchResponse> {
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(filters)) {
    if (v !== undefined && v !== "" && v !== null) params.set(k, String(v));
  }
  params.set("view_mode", viewMode);
  const qs = params.toString();
  return request<UnifiedSearchResponse>(`/search${qs ? `?${qs}` : ""}`, identity, { signal });
}

// --- Staffing Continuity Intelligence — HR in WORK mode only. Every call
// here 403s for any other (role, view_mode); App.tsx never renders the
// calling UI at all outside that pair.
//
// viewMode is a real argument, not decoration: employee mode is "what an
// ordinary colleague sees", and work-authorization review dates are exactly
// what an ordinary colleague must not see. The server decides (it collapses
// every role to employee in that mode) — this just stops the UI asking for
// something it will be refused.

export function getContinuityOverview(
  identity: Identity, viewMode: ViewMode, windowDays?: number,
): Promise<ContinuityOverview> {
  const params = new URLSearchParams({ view_mode: viewMode });
  if (windowDays !== undefined) params.set("window_days", String(windowDays));
  return request<ContinuityOverview>(`/continuity/exposure?${params}`, identity);
}

export interface ContinuityFilters {
  exposure?: string;
  client?: string;
  project?: string;
  office?: string;
  org_unit?: string;
  dependency_type?: string;
  window_days?: number;
}

export function getEngagementExposure(
  identity: Identity, viewMode: ViewMode, filters: ContinuityFilters = {},
): Promise<EngagementExposure[]> {
  const params = new URLSearchParams({ view_mode: viewMode });
  for (const [k, v] of Object.entries(filters)) {
    if (v !== undefined && v !== "") params.set(k, String(v));
  }
  return request<EngagementExposure[]>(`/continuity/engagement-exposure?${params}`, identity);
}

export async function getEmployeeContinuity(
  identity: Identity, employeeId: string, viewMode: ViewMode,
): Promise<EmployeeContinuityDetail | null> {
  try {
    return await request<EmployeeContinuityDetail>(
      `/continuity/employees/${employeeId}?view_mode=${viewMode}`, identity);
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return null;
    throw e;
  }
}

export interface HrReviewQueueFilters {
  authorization_type?: string;
  exposure?: string;
  next_review_from?: string;
  next_review_to?: string;
  engagements_min?: number;
  engagements_max?: number;
  window_days?: number;
}

export function getHrReviewQueue(
  identity: Identity, viewMode: ViewMode, filters: HrReviewQueueFilters = {},
): Promise<HrReviewQueueItem[]> {
  const params = new URLSearchParams({ view_mode: viewMode });
  for (const [k, v] of Object.entries(filters)) {
    if (v !== undefined && v !== "") params.set(k, String(v));
  }
  return request<HrReviewQueueItem[]>(`/continuity/review-queue?${params}`, identity);
}

// --- AI-assisted doc upload for HR — HR-only, work mode only. Every call
// here 403s for any other (role, view_mode); ReviewPage never renders the
// calling UI at all outside that pair, same non-visibility guarantee
// Continuity's HR-only calls above have.

// Bespoke fetch rather than the generic request<T> helper: a multipart
// upload must NOT set Content-Type itself — the browser has to compute the
// multipart boundary and set the header for us, and passing one explicitly
// (even matching) breaks that.
export async function uploadDoc(
  identity: Identity, file: File, viewMode: ViewMode,
): Promise<UploadDocResult> {
  const form = new FormData();
  form.append("file", file);
  const res = await fetch(`${API_BASE}/docs/upload?view_mode=${viewMode}`, {
    method: "POST",
    headers: headers(identity),
    body: form,
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => null);
    throw new ApiError(res.status, detail?.detail ?? `${res.status} ${res.statusText}`);
  }
  return res.json() as Promise<UploadDocResult>;
}

export function listUploadedDocs(
  identity: Identity, viewMode: ViewMode,
): Promise<{ documents: UploadedDocSummary[] }> {
  return request(`/uploaded_docs?view_mode=${viewMode}`, identity);
}

export function finalizeDocument(
  identity: Identity, docId: number, acceptIds: number[], viewMode: ViewMode,
): Promise<{ doc_id: number; results: BulkResultRow[]; content_scrubbed_at: string }> {
  return request(`/docs/${docId}/finalize?view_mode=${viewMode}`, identity, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ accept_ids: acceptIds }),
  });
}

export function listDocSubjectMatches(
  identity: Identity, viewMode: ViewMode, filters: { docId?: number; status?: string } = {},
): Promise<{ doc_id: number | null; subjects: DocSubjectMatchOut[] }> {
  const params = new URLSearchParams({ view_mode: viewMode });
  if (filters.docId !== undefined) params.set("doc_id", String(filters.docId));
  if (filters.status) params.set("status", filters.status);
  return request(`/doc_subject_matches?${params.toString()}`, identity);
}

export function resolveDocSubjectMatch(
  identity: Identity, subjectId: number,
  body: { employee_id: string } | { new_hire: true },
  viewMode: ViewMode,
): Promise<DocSubjectMatchOut> {
  return request(`/doc_subject_matches/${subjectId}/resolve?view_mode=${viewMode}`, identity, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export function listProposedChanges(
  identity: Identity, viewMode: ViewMode,
  filters: { docId?: number; employeeId?: string; status?: string } = {},
): Promise<{ doc_id: number | null; groups: ProposedChangeGroup[] }> {
  const params = new URLSearchParams({ view_mode: viewMode });
  if (filters.docId !== undefined) params.set("doc_id", String(filters.docId));
  if (filters.employeeId) params.set("employee_id", filters.employeeId);
  if (filters.status) params.set("status", filters.status);
  return request(`/proposed_changes?${params.toString()}`, identity);
}

function proposedChangeAction(
  identity: Identity, proposalId: number, action: string, viewMode: ViewMode, body?: unknown,
) {
  return request(`/proposed_changes/${proposalId}/${action}?view_mode=${viewMode}`, identity, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
}

export const acceptProposedChange = (identity: Identity, id: number, viewMode: ViewMode) =>
  proposedChangeAction(identity, id, "accept", viewMode);

export const editProposedChange = (
  identity: Identity, id: number, editedValue: Record<string, unknown>, viewMode: ViewMode,
) => proposedChangeAction(identity, id, "edit", viewMode, { edited_value: editedValue });

export const rejectProposedChange = (identity: Identity, id: number, viewMode: ViewMode) =>
  proposedChangeAction(identity, id, "reject", viewMode);

// Only valid on an accepted/edited row, and only while its source document
// hasn't been finalized yet — see app/proposals.py's undo().
export const undoProposedChange = (identity: Identity, id: number, viewMode: ViewMode) =>
  proposedChangeAction(identity, id, "undo", viewMode);

export const reassignProposedChange = (
  identity: Identity, id: number, employeeId: string, viewMode: ViewMode,
) => proposedChangeAction(identity, id, "reassign", viewMode, { employee_id: employeeId });

interface BulkSelector {
  ids?: number[];
  docId?: number;
  employeeId?: string;
}

function bulkProposedChangeAction(
  identity: Identity, action: string, viewMode: ViewMode, selector: BulkSelector,
): Promise<{ results: BulkResultRow[] }> {
  return request(`/proposed_changes/${action}?view_mode=${viewMode}`, identity, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      ids: selector.ids, doc_id: selector.docId, employee_id: selector.employeeId,
    }),
  });
}

export const bulkAcceptProposedChanges = (identity: Identity, viewMode: ViewMode, selector: BulkSelector) =>
  bulkProposedChangeAction(identity, "bulk_accept", viewMode, selector);

export const bulkRejectProposedChanges = (identity: Identity, viewMode: ViewMode, selector: BulkSelector) =>
  bulkProposedChangeAction(identity, "bulk_reject", viewMode, selector);

// --- Community Graph — every call here is implicitly scoped to `identity`'s
// own graph; there is no person-id parameter anywhere below that could ask
// for someone else's (see app/community_links.py's visibility guarantee).

// viewMode is forwarded because the server drops links whose contact is no
// longer visible, using the same check getPerson applies — passing a
// different mode to each would hand back a contact the profile lookup then
// refuses, which is what used to render a bare id in the graph.
export function listCommunityLinks(
  identity: Identity, viewMode: ViewMode,
): Promise<CommunityLinkOut[]> {
  return request<CommunityLinkOut[]>(`/community_links?view_mode=${viewMode}`, identity);
}

export function createCommunityLink(
  identity: Identity, body: { contact_employee_id: string; role_label: string; reason: string },
): Promise<CommunityLinkOut> {
  return request<CommunityLinkOut>("/community_links", identity, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export function updateCommunityLink(
  identity: Identity, linkId: number, changes: { role_label?: string; reason?: string },
): Promise<CommunityLinkOut> {
  return request<CommunityLinkOut>(`/community_links/${linkId}`, identity, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(changes),
  });
}

export async function deleteCommunityLink(identity: Identity, linkId: number): Promise<void> {
  const res = await fetch(`${API_BASE}/community_links/${linkId}`, {
    method: "DELETE",
    headers: headers(identity),
  });
  if (!res.ok) {
    throw new ApiError(res.status, `${res.status} ${res.statusText}`);
  }
}

// --- HR review queue for bootstrapped official-link suggestions. Every
// call here 403s for a non-"hr" identity; CommunityPage never renders the
// review section at all outside that role, same non-visibility guarantee
// Continuity's HR-only calls carry.

export function listSuggestedOfficialLinks(
  identity: Identity, viewMode: ViewMode, officeId?: number,
): Promise<SuggestedOfficialLinkOut[]> {
  const params = new URLSearchParams({ view_mode: viewMode });
  if (officeId !== undefined) params.set("office_id", String(officeId));
  return request<SuggestedOfficialLinkOut[]>(`/suggested_official_links?${params}`, identity);
}

export function generateSuggestedOfficialLinks(
  identity: Identity, viewMode: ViewMode,
): Promise<SuggestedOfficialLinkOut[]> {
  return request<SuggestedOfficialLinkOut[]>(
    `/suggested_official_links/generate?view_mode=${viewMode}`, identity, { method: "POST" });
}

export function confirmSuggestedOfficialLink(
  identity: Identity, id: number, viewMode: ViewMode,
): Promise<SuggestedOfficialLinkOut> {
  return request<SuggestedOfficialLinkOut>(
    `/suggested_official_links/${id}/confirm?view_mode=${viewMode}`, identity, { method: "POST" });
}

export function rejectSuggestedOfficialLink(
  identity: Identity, id: number, viewMode: ViewMode,
): Promise<SuggestedOfficialLinkOut> {
  return request<SuggestedOfficialLinkOut>(
    `/suggested_official_links/${id}/reject?view_mode=${viewMode}`, identity, { method: "POST" });
}

// Mentor auto-assignment sweep for new hires -- unlike the office/role
// suggestions above, this creates the official mentor link directly (no
// confirm step); see app/community_links.py's auto_assign_mentors for why.
// HR-only, same gate as the rest of this section.
export function autoAssignMentors(
  identity: Identity, viewMode: ViewMode,
): Promise<CommunityLinkOut[]> {
  return request<CommunityLinkOut[]>(
    `/community_links/auto_assign_mentors?view_mode=${viewMode}`, identity, { method: "POST" });
}
