import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, readCookie, request } from "../api/client";
import { api } from "../api/endpoints";
import i18n from "../i18n";
import { formatBytes, formatDate, formatRelative } from "./format";

afterEach(async () => {
  vi.unstubAllGlobals();
  await i18n.changeLanguage("en");
});

describe("format", () => {
  it("formats sizes", () => {
    expect(formatBytes(0)).toBe("0 B");
    expect(formatBytes(1536)).toBe("1.5 KB");
    expect(formatBytes(150 * 1024 ** 2)).toBe("150 MB");
    expect(formatBytes(3 * 1024 ** 5)).toBe("3,072 TB");
  });

  it("formats dates and relative times", () => {
    expect(formatDate(null)).toBe("—");
    expect(formatDate("2026-11-18T09:00:00Z")).toMatch(/2026|26/);
    const now = Date.parse("2026-11-18T12:00:00Z");
    expect(formatRelative(undefined)).toBe("—");
    expect(formatRelative("2026-11-18T11:59:30Z", now)).toBe("30 seconds ago");
    expect(formatRelative("2026-11-18T11:55:00Z", now)).toBe("5 minutes ago");
    expect(formatRelative("2026-11-18T09:00:00Z", now)).toBe("3 hours ago");
    expect(formatRelative("2026-11-16T12:00:00Z", now)).toBe("2 days ago");
  });
});

describe("api client", () => {
  function stub(response: object) {
    const fn = vi.fn().mockResolvedValue(response);
    vi.stubGlobal("fetch", fn);
    return fn;
  }

  it("keeps the status text when the error body is not JSON", async () => {
    stub({
      ok: false,
      status: 502,
      statusText: "Bad Gateway",
      json: () => Promise.reject(new Error("x")),
    });
    const err = await request("GET", "/api/x").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).detail).toBe("Bad Gateway");
    expect((err as ApiError).message).toBe("HTTP 502: Bad Gateway");
  });

  it("keeps the status text when detail is structured", async () => {
    stub({
      ok: false,
      status: 422,
      statusText: "Unprocessable",
      json: () => Promise.resolve({ detail: [{}] }),
    });
    await expect(request("GET", "/api/x")).rejects.toThrow(
      "HTTP 422: Unprocessable",
    );
  });

  it("builds query strings and calls health", async () => {
    const fn = stub({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ status: "ok", version: "1" }),
    });
    await api.health();
    await request("GET", "/api/x", undefined, {
      a: 1,
      b: undefined,
      c: "",
      d: true,
    });
    expect(fn).toHaveBeenLastCalledWith(
      "/api/x?a=1&d=true",
      expect.objectContaining({ method: "GET" }),
    );
  });

  it("reads cookies", () => {
    document.cookie = "other=1";
    expect(readCookie("other")).toBe("1");
    expect(readCookie("missing")).toBeUndefined();
  });
});
