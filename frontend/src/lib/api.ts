// Thin fetch wrapper for the Django API. The API is served from the same origin (Next.js rewrites
// or a reverse proxy), so the session cookie is first-party and unsafe requests carry the CSRF token.

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public details: unknown = null,
  ) {
    super(message);
  }

  fieldErrors(): Record<string, string> {
    const out: Record<string, string> = {};
    if (this.details && typeof this.details === "object" && !Array.isArray(this.details)) {
      for (const [key, value] of Object.entries(this.details as Record<string, unknown>)) {
        out[key] = Array.isArray(value) ? String(value[0]) : String(value);
      }
    }
    return out;
  }
}

const UNSAFE = new Set(["POST", "PUT", "PATCH", "DELETE"]);

function readCookie(name: string): string | null {
  if (typeof document === "undefined") return null;
  const match = document.cookie.split("; ").find((row) => row.startsWith(`${name}=`));
  return match ? decodeURIComponent(match.split("=")[1]) : null;
}

let csrfPromise: Promise<void> | null = null;

async function ensureCsrf(): Promise<string> {
  let token = readCookie("csrftoken");
  if (!token) {
    csrfPromise ??= fetch("/api/v1/auth/csrf", { credentials: "same-origin" }).then(() => undefined);
    await csrfPromise;
    csrfPromise = null;
    token = readCookie("csrftoken");
  }
  return token ?? "";
}

export async function api<T>(path: string, init: { method?: string; body?: unknown } = {}): Promise<T> {
  const method = (init.method ?? "GET").toUpperCase();
  const headers: Record<string, string> = { Accept: "application/json" };
  if (init.body !== undefined) headers["Content-Type"] = "application/json";
  if (UNSAFE.has(method)) headers["X-CSRFToken"] = await ensureCsrf();

  const response = await fetch(`/api/v1${path}`, {
    method,
    headers,
    credentials: "same-origin",
    body: init.body !== undefined ? JSON.stringify(init.body) : undefined,
  });
  if (response.status === 204) return undefined as T;
  const text = await response.text();
  const data = text ? safeJson(text) : null;
  if (!response.ok) {
    const error = (data as { error?: { code?: string; message?: string; details?: unknown } } | null)?.error;
    throw new ApiError(
      response.status,
      error?.code ?? "http_error",
      error?.message ?? `Request failed (${response.status})`,
      error?.details ?? null,
    );
  }
  return data as T;
}

function safeJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

export function isUnauthenticated(error: unknown): boolean {
  return error instanceof ApiError && error.status === 401;
}
