import type { AskResponse, Identity, OrgChainNode, PersonDetail, PersonSummary } from "./types";

// Defaults to the local backend for normal dev. Override with
// VITE_API_BASE (see package.json's "dev:live" script) to point this same
// frontend at the deployed Azure backend instead -- e.g. to view real
// deployed data without running uvicorn locally.
const API_BASE = import.meta.env.VITE_API_BASE ?? "http://127.0.0.1:8000";

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

export function findPeople(identity: Identity, filters: SearchFilters, signal?: AbortSignal): Promise<PersonSummary[]> {
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(filters)) {
    if (v !== undefined && v !== "" && v !== null) params.set(k, String(v));
  }
  const qs = params.toString();
  return request<PersonSummary[]>(`/people${qs ? `?${qs}` : ""}`, identity, { signal });
}

export async function getPerson(identity: Identity, personId: string): Promise<PersonDetail | null> {
  try {
    return await request<PersonDetail>(`/people/${personId}`, identity);
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return null;
    throw e;
  }
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

export function ask(identity: Identity, message: string): Promise<AskResponse> {
  return request<AskResponse>("/ask", identity, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message }),
  });
}
