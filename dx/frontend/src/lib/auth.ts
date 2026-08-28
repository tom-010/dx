/**
 * Access-token storage for the SPA. The API requires `Authorization: Bearer <token>` on every
 * call (issued by POST /api/auth/login); token-based auth is shared with the native builds.
 * `customFetch` reads the token, `__root.tsx` redirects to /login when there is none.
 */
import { useSyncExternalStore } from "react";

const STORAGE_KEY = "dx.access_token";
const listeners = new Set<() => void>();

function readStoredToken(): string | null {
  try {
    return localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

let accessToken = readStoredToken();

export function getAccessToken(): string | null {
  return accessToken;
}

export function setAccessToken(token: string | null): void {
  accessToken = token;
  try {
    if (token === null) localStorage.removeItem(STORAGE_KEY);
    else localStorage.setItem(STORAGE_KEY, token);
  } catch {
    // Storage unavailable (private mode, quota): the token still lives in memory.
  }
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Reactive access token; re-renders when the user logs in/out or a 401 clears it. */
export function useAccessToken(): string | null {
  return useSyncExternalStore(subscribe, getAccessToken, getAccessToken);
}
