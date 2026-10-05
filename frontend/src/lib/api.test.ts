import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, api, isUnauthenticated } from "./api";

function jsonResponse(status: number, body: unknown) {
  return new Response(body === undefined ? null : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("api()", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
    document.cookie = "csrftoken=abc123";
  });

  afterEach(() => {
    fetchMock.mockReset();
    vi.unstubAllGlobals();
  });

  it("returns parsed JSON for GET without a CSRF header", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, { id: 1 }));
    await expect(api("/auth/me")).resolves.toEqual({ id: 1 });
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/v1/auth/me");
    expect(init.headers["X-CSRFToken"]).toBeUndefined();
  });

  it("sends the CSRF token and JSON body on unsafe requests", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(201, { ok: true }));
    await api("/llm-credentials", { method: "POST", body: { name: "x" } });
    const [, init] = fetchMock.mock.calls[0];
    expect(init.headers["X-CSRFToken"]).toBe("abc123");
    expect(init.headers["Content-Type"]).toBe("application/json");
    expect(init.body).toBe('{"name":"x"}');
  });

  it("returns undefined for 204", async () => {
    fetchMock.mockResolvedValueOnce(new Response(null, { status: 204 }));
    await expect(api("/auth/logout", { method: "POST" })).resolves.toBeUndefined();
  });

  it("raises ApiError with the server's error envelope", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(400, {
        error: { code: "validation_error", message: "api_key: Validation failed", details: { api_key: ["Validation failed"] } },
      }),
    );
    const error = await api("/llm-credentials", { method: "POST", body: {} }).catch((e) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(400);
    expect(error.code).toBe("validation_error");
    expect(error.fieldErrors()).toEqual({ api_key: "Validation failed" });
  });

  it("flags 401 as unauthenticated", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(401, { error: { code: "not_authenticated", message: "no" } }));
    const error = await api("/auth/me").catch((e) => e);
    expect(isUnauthenticated(error)).toBe(true);
  });
});
