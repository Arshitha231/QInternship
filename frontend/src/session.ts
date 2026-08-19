import { API_BASE } from "./api";
import type { Identity } from "./types";

// The signed-in identity, persisted so a page refresh (or the browser
// history navigation the profile stack does) doesn't drop you back at the
// login form.
//
// Stored WITH the API base it was issued against, and discarded on
// mismatch. Employee ids are per-database -- seed.py draws names from a
// fixed RNG seed but ids from uuid4, so the local sqlite file and the
// deployed Azure SQL database hold the same people under different ids.
// A session saved while running against localhost and replayed against
// tempest34 would carry an id that resolves to nobody there, and every
// profile read would 404 with the user apparently logged in fine. That
// exact failure is what frontend/src/identities.ts used to be about.
//
// sessionStorage, not localStorage, is deliberate: a demo laptop that gets
// closed and reopened should land on the login page, because the login page
// is part of what's being demoed.

const STORAGE_KEY = "orghub.session";

interface StoredSession {
  identity: Identity;
  apiBase: string;
}

export function loadSession(): Identity | null {
  const raw = sessionStorage.getItem(STORAGE_KEY);
  if (!raw) return null;
  try {
    const stored = JSON.parse(raw) as StoredSession;
    if (stored.apiBase !== API_BASE) {
      sessionStorage.removeItem(STORAGE_KEY);
      return null;
    }
    const { id, role, name } = stored.identity ?? {};
    if (!id || !role || !name) return null;
    return { id, role, name };
  } catch {
    sessionStorage.removeItem(STORAGE_KEY);
    return null;
  }
}

export function saveSession(identity: Identity): void {
  const payload: StoredSession = { identity, apiBase: API_BASE };
  sessionStorage.setItem(STORAGE_KEY, JSON.stringify(payload));
}

export function clearSession(): void {
  sessionStorage.removeItem(STORAGE_KEY);
}
