/**
 * Transport used by every generated API function (see orval.config.ts → override.mutator).
 * Hand-written on purpose: it is not part of the API contract, only the HTTP plumbing.
 *
 * Auth: adds the access token (src/lib/auth.ts) as bearer header. When the API answers 401 the
 * token has expired — the refresh token is traded for a new pair (POST /api/auth/refresh) and
 * the request is retried once. Only when that fails too is the session over: the tokens are
 * dropped and the root route sends the user back to /login.
 */
import {
  getRefreshTokenUrl,
  refreshToken as requestRefresh,
} from "@/api/auth/auth";
import {
  getAccessToken,
  getRefreshToken,
  reloadTokens,
  setTokens,
} from "@/lib/auth";

/** Non-2xx responses are thrown so TanStack Query surfaces them as errors. */
export class ApiError extends Error {
  readonly status: number;
  readonly detail: unknown;

  constructor(status: number, detail: unknown) {
    super(
      typeof detail === "string"
        ? detail
        : `Request failed with status ${status}`,
    );
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

// Empty in the web build (same origin); the native (Capacitor) build sets an absolute URL.
const API_URL: string = import.meta.env.VITE_API_URL ?? "";

/** Absolute URL for an API path the backend returned (`stream_url`, `download_url`) — for
 * browser APIs that bypass `customFetch`, such as `EventSource` or `<a href>`. */
export function apiUrl(path: string): string {
  return `${API_URL}${path}`;
}

async function parseBody(response: Response): Promise<unknown> {
  if (response.status === 204) return undefined;
  const text = await response.text();
  if (text.length === 0) return undefined;
  const contentType = response.headers.get("content-type") ?? "";
  return contentType.includes("application/json") ? JSON.parse(text) : text;
}

function send(
  url: string,
  init: RequestInit | undefined,
  token: string | null,
): Promise<Response> {
  const headers = new Headers(init?.headers);
  if (token !== null && !headers.has("Authorization")) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  return fetch(apiUrl(url), { ...init, headers });
}

let refreshing: Promise<string | null> | null = null;

/**
 * New access token from the refresh token, or null when the session is over. Single-flight:
 * requests failing at the same moment share one refresh call (the refresh token is single-use).
 */
function refreshSession(): Promise<string | null> {
  refreshing ??= refreshOnce().finally((): void => {
    refreshing = null;
  });
  return refreshing;
}

async function refreshOnce(): Promise<string | null> {
  const refresh = getRefreshToken();
  if (refresh === null) return null;
  try {
    const pair = await requestRefresh({ refresh_token: refresh });
    setTokens(pair);
    return pair.access_token;
  } catch (error) {
    if (!(error instanceof ApiError) || error.status !== 401) {
      throw error; // network trouble: keep the session, the caller reports the failure
    }
    // Rejected. Either another tab rotated the pair meanwhile (its copy is in storage), or
    // the session has ended (expired, logged out elsewhere, revoked).
    if (reloadTokens()) return getAccessToken();
    setTokens(null);
    return null;
  }
}

export async function customFetch<T>(
  url: string,
  init?: RequestInit,
): Promise<T> {
  const token = getAccessToken();
  let response = await send(url, init, token);
  if (
    response.status === 401 &&
    token !== null &&
    url !== getRefreshTokenUrl()
  ) {
    // Expired access token: use the one a concurrent refresh already stored, else refresh now.
    const current = getAccessToken();
    const fresh =
      current !== null && current !== token ? current : await refreshSession();
    if (fresh !== null) response = await send(url, init, fresh);
  }
  const body = await parseBody(response);
  if (!response.ok) {
    const detail =
      typeof body === "object" && body !== null && "detail" in body
        ? (body as { detail: unknown }).detail
        : body;
    throw new ApiError(response.status, detail);
  }
  return body as T;
}

/** Human-readable message for errors surfaced by TanStack Query (typed `unknown` by orval). */
export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    return typeof error.detail === "string"
      ? error.detail
      : `${error.message} (${error.status})`;
  }
  return error instanceof Error ? error.message : String(error);
}
