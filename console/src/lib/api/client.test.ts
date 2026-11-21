import { afterEach, describe, expect, it, vi } from "vitest";
import { setOwnerToken } from "../session";
import { ApiError, apiRequest, buildQuery, describeError } from "./client";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("buildQuery", () => {
  it("drops empty values and encodes the rest", () => {
    expect(buildQuery({ a: 1, b: "", c: undefined, d: "x y", e: false })).toBe("?a=1&d=x+y&e=false");
    expect(buildQuery({})).toBe("");
  });
});

describe("describeError", () => {
  it("joins validation messages from the server envelope", () => {
    const error = new ApiError(422, "validation_error", "请求无效", [{ msg: "field required" }, { msg: "too short" }]);
    expect(describeError(error)).toBe("请求无效：field required；too short");
  });

  it("uses the server message for other API errors", () => {
    expect(describeError(new ApiError(403, "forbidden", "Access denied"))).toBe("Access denied");
  });

  it("handles unknown values", () => {
    expect(describeError("boom")).toBe("发生未知错误");
  });
});

describe("apiRequest", () => {
  it("refuses to send a request without the owner credential", async () => {
    await expect(apiRequest("/api/v1/memories")).rejects.toMatchObject({ status: 401, code: "unauthorized" });
  });

  it("sends the credential and the active space, then returns the payload", async () => {
    setOwnerToken("owner-token-12345");
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ data: [] }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const payload = await apiRequest<{ data: unknown[] }>("/api/v1/memories", { query: { page: 2 } });
    expect(payload).toEqual({ data: [] });
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/v1/memories?page=2");
    const headers = new Headers(init.headers);
    expect(headers.get("Authorization")).toBe("Bearer owner-token-12345");
    expect(headers.get("X-JarvisMind-Space")).toBe("default");
  });

  it("normalises the server error envelope", async () => {
    setOwnerToken("owner-token-12345");
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(
            JSON.stringify({ error: { code: "conflict", message: "already exists", details: { code: "x" } } }),
            { status: 409 },
          ),
      ),
    );
    await expect(apiRequest("/api/v1/control/spaces")).rejects.toMatchObject({
      status: 409,
      code: "conflict",
      message: "already exists",
    });
  });
});
