/**
 * Token storage for the SPA. A login (POST /api/auth/login) yields a pair: the short-lived
 * access token that `customFetch` sends as `Authorization: Bearer …` on every call, and the
 * refresh token it trades in for a new pair once the access token has expired
 * (see `custom-fetch.ts`). `__root.tsx` redirects to /login while there is no access token.
 *
 * Both live in localStorage so a reload keeps the session; the `storage` event keeps every
 * open tab on the same pair (one tab refreshing or logging out updates the others).
 */
import { useSyncExternalStore } from "react";
import type { TokenOut } from "@/api/model";

const ACCESS_KEY = "dx.access_token";
const REFRESH_KEY = "dx.refresh_token";
const listeners = new Set<() => void>();

function readStoredTokens(): TokenOut | null {
  try {
    const access = localStorage.getItem(ACCESS_KEY);
    const refresh = localStorage.getItem(REFRESH_KEY);
    return access !== null && refresh !== null
      ? { access_token: access, refresh_token: refresh }
      : null;
  } catch {
    return null;
  }
}

let tokens: TokenOut | null = readStoredTokens();

function notify(): void {
  for (const listener of listeners) listener();
}

export function getAccessToken(): string | null {
  return tokens?.access_token ?? null;
}

export function getRefreshToken(): string | null {
  return tokens?.refresh_token ?? null;
}

/** Store a freshly issued pair (login, refresh) or forget the session (`null`). */
export function setTokens(pair: TokenOut | null): void {
  tokens = pair;
  try {
    if (pair === null) {
      localStorage.removeItem(ACCESS_KEY);
      localStorage.removeItem(REFRESH_KEY);
    } else {
      localStorage.setItem(ACCESS_KEY, pair.access_token);
      localStorage.setItem(REFRESH_KEY, pair.refresh_token);
    }
  } catch {
    // Storage unavailable (private mode, quota): the tokens still live in memory.
  }
  notify();
}

/**
 * Re-read what another tab stored. Returns true when that changed the pair — used by the
 * refresh flow: a refresh token rejected here may simply have been rotated by another tab.
 */
export function reloadTokens(): boolean {
  const stored = readStoredTokens();
  const changed =
    stored?.access_token !== tokens?.access_token ||
    stored?.refresh_token !== tokens?.refresh_token;
  if (changed) {
    tokens = stored;
    notify();
  }
  return changed;
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

if (typeof window !== "undefined") {
  window.addEventListener("storage", (event: StorageEvent): void => {
    if (
      event.key === null ||
      event.key === ACCESS_KEY ||
      event.key === REFRESH_KEY
    ) {
      reloadTokens();
    }
  });
}

/** Reactive access token; re-renders when the user logs in/out or the session ends. */
export function useAccessToken(): string | null {
  return useSyncExternalStore(subscribe, getAccessToken, getAccessToken);
}
