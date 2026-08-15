import type {
  ContinuityOverview, EmployeeContinuityDetail, EngagementExposure, HrReviewQueueItem,
  Identity, NotificationOut, OrgChainNode, PersonDetail, PersonSummary, UnifiedSearchResponse,
  UpdateEmployeeChanges, ViewMode,
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
// Add to api.ts
import type { OrgChainNode } from "./types";

export interface TeamProjectOut {
  id: number;
  name: string;
  classification: string;
}

export interface TeammateOut {
  project_id: number;
  person: OrgChainNode;
}

export interface TeamGraphResponse {
  projects: TeamProjectOut[];
  teammates: TeammateOut[];
}

export async function getTeamGraph(identity: Identity, personId: string): Promise<TeamGraphResponse | null> {
  try {
    return await request<TeamGraphResponse>(`/people/${personId}/team-graph`, identity);
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return null;
    throw e;
  }
}
