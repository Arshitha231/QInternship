import type {
  BulkResultRow, CommunityLinkOut, ContinuityOverview, DocSubjectMatchOut, EmployeeContinuityDetail,
  EngagementExposure, HrReviewQueueItem, Identity, NotificationOut, OrgChainNode, PersonDetail,
  PersonSummary, ProposedChangeGroup, SuggestedOfficialLinkOut, UnifiedSearchResponse,
  UpdateEmployeeChanges, UploadDocResult, UploadedDocSummary, ViewMode,
} from "./types";

// Defaults to the local backend for normal dev. Override with
// VITE_API_BASE (see package.json's "dev:live" script) to point this same
// frontend at the deployed Azure backend instead -- e.g. to view real
// deployed data without running uvicorn locally.
export const API_BASE = import.meta.env.VITE_API_BASE ?? "http://127.0.0.1:8000";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
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
    throw new ApiError(res.status, `${res.status} ${res.statusText}`);
  }
  return res.json() as Promise<T>;
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

export async function getOrgChart(
  identity: Identity,
  personId: string,
  direction: "up" | "down",
  depth = 10,
): Promise<OrgChainNode[]> {
  try {
    return await request<OrgChainNode[]>(`/people/${personId}/org-chart?direction=${direction}&depth=${depth}`, identity);
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

// --- Staffing Continuity Intelligence — HR-only. Every call here 403s for
// a non-"hr" identity; App.tsx never renders the calling UI at all for one.

export function getContinuityOverview(identity: Identity, windowDays?: number): Promise<ContinuityOverview> {
  const qs = windowDays !== undefined ? `?window_days=${windowDays}` : "";
  return request<ContinuityOverview>(`/continuity/exposure${qs}`, identity);
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
  identity: Identity, filters: ContinuityFilters = {},
): Promise<EngagementExposure[]> {
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(filters)) {
    if (v !== undefined && v !== "") params.set(k, String(v));
  }
  const qs = params.toString();
  return request<EngagementExposure[]>(`/continuity/engagement-exposure${qs ? `?${qs}` : ""}`, identity);
}

export async function getEmployeeContinuity(
  identity: Identity, employeeId: string,
): Promise<EmployeeContinuityDetail | null> {
  try {
    return await request<EmployeeContinuityDetail>(`/continuity/employees/${employeeId}`, identity);
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return null;
    throw e;
  }
}

export function getHrReviewQueue(identity: Identity, windowDays?: number): Promise<HrReviewQueueItem[]> {
  const qs = windowDays !== undefined ? `?window_days=${windowDays}` : "";
  return request<HrReviewQueueItem[]>(`/continuity/review-queue${qs}`, identity);
}

// --- AI-assisted doc upload for IT — IT-only, work mode only. Every call
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

export function listCommunityLinks(identity: Identity): Promise<CommunityLinkOut[]> {
  return request<CommunityLinkOut[]>("/community_links", identity);
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
  identity: Identity, officeId?: number,
): Promise<SuggestedOfficialLinkOut[]> {
  const qs = officeId !== undefined ? `?office_id=${officeId}` : "";
  return request<SuggestedOfficialLinkOut[]>(`/suggested_official_links${qs}`, identity);
}

export function generateSuggestedOfficialLinks(identity: Identity): Promise<SuggestedOfficialLinkOut[]> {
  return request<SuggestedOfficialLinkOut[]>("/suggested_official_links/generate", identity, { method: "POST" });
}

export function confirmSuggestedOfficialLink(identity: Identity, id: number): Promise<SuggestedOfficialLinkOut> {
  return request<SuggestedOfficialLinkOut>(`/suggested_official_links/${id}/confirm`, identity, { method: "POST" });
}

export function rejectSuggestedOfficialLink(identity: Identity, id: number): Promise<SuggestedOfficialLinkOut> {
  return request<SuggestedOfficialLinkOut>(`/suggested_official_links/${id}/reject`, identity, { method: "POST" });
}

// Mentor auto-assignment sweep for new hires -- unlike the office/role
// suggestions above, this creates the official mentor link directly (no
// confirm step); see app/community_links.py's auto_assign_mentors for why.
// HR-only, same gate as the rest of this section.
export function autoAssignMentors(identity: Identity): Promise<CommunityLinkOut[]> {
  return request<CommunityLinkOut[]>("/community_links/auto_assign_mentors", identity, { method: "POST" });
}
