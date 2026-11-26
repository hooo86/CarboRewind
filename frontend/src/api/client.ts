/** Minimal API client: same-origin cookies, CSRF header on unsafe methods, typed errors. */

export const CSRF_COOKIE = "__Host-carborewind_csrf";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly detail: string,
  ) {
    super(`HTTP ${status}: ${detail}`);
  }
}

type Listener = () => void;
const unauthorizedListeners = new Set<Listener>();

/** Called whenever the API answers 401 (session expired or revoked). */
export function onUnauthorized(listener: Listener): () => void {
  unauthorizedListeners.add(listener);
  return () => unauthorizedListeners.delete(listener);
}

export function readCookie(name: string): string | undefined {
  return document.cookie
    .split("; ")
    .find((c) => c.startsWith(`${name}=`))
    ?.slice(name.length + 1);
}

let csrfToken: string | undefined;

/** The login answer carries the CSRF token; the cookie is the fallback after a reload. */
export function setCsrfToken(token: string | undefined): void {
  csrfToken = token;
}

const UNSAFE = new Set(["POST", "PUT", "PATCH", "DELETE"]);

export async function request<T>(
  method: string,
  path: string,
  body?: unknown,
  query?: Record<string, string | number | boolean | string[] | undefined>,
): Promise<T> {
  const url = new URL(path, window.location.origin);
  for (const [key, value] of Object.entries(query ?? {})) {
    if (Array.isArray(value))
      value.forEach((v) => url.searchParams.append(key, v));
    else if (value !== undefined && value !== "")
      url.searchParams.set(key, String(value));
  }
  const headers: Record<string, string> = { Accept: "application/json" };
  // A Blob (file upload) is sent as is; anything else as JSON.
  const raw = typeof Blob !== "undefined" && body instanceof Blob;
  if (body !== undefined)
    headers["Content-Type"] = raw
      ? "application/octet-stream"
      : "application/json";
  if (UNSAFE.has(method)) {
    const token = csrfToken ?? readCookie(CSRF_COOKIE);
    if (token) headers["X-CSRF-Token"] = token;
  }
  const response = await fetch(url.pathname + url.search, {
    method,
    headers,
    credentials: "same-origin",
    body:
      body === undefined
        ? undefined
        : raw
          ? (body as Blob)
          : JSON.stringify(body),
  });
  if (response.status === 401) {
    unauthorizedListeners.forEach((listener) => listener());
  }
  if (!response.ok) {
    // HTTP/2 has no reason phrase: fall back on the status code.
    let detail = response.statusText || `HTTP ${response.status}`;
    try {
      const payload = (await response.json()) as { detail?: unknown };
      if (typeof payload.detail === "string" && payload.detail)
        detail = payload.detail;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(response.status, detail);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}
