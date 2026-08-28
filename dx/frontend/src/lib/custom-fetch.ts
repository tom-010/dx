/**
 * Transport used by every generated API function (see orval.config.ts → override.mutator).
 * Hand-written on purpose: it is not part of the API contract, only the HTTP plumbing.
 * Adds the bearer token (src/lib/auth.ts) and drops it again when the API answers 401.
 */
import { getAccessToken, setAccessToken } from "@/lib/auth";

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

export async function customFetch<T>(
  url: string,
  init?: RequestInit,
): Promise<T> {
  const token = getAccessToken();
  const headers = new Headers(init?.headers);
  if (token !== null && !headers.has("Authorization")) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  const response = await fetch(apiUrl(url), { ...init, headers });
  const body = await parseBody(response);
  if (response.status === 401 && token !== null) {
    // Expired or revoked: forget it so the root route sends the user back to /login.
    setAccessToken(null);
  }
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
