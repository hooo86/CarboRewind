import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import { fetchHealth } from "./api";
import i18n from "./i18n";

function mockFetch(response: Partial<Response> | Error) {
  const fn =
    response instanceof Error
      ? vi.fn().mockRejectedValue(response)
      : vi.fn().mockResolvedValue(response);
  vi.stubGlobal("fetch", fn);
  return fn;
}

afterEach(async () => {
  vi.unstubAllGlobals();
  await i18n.changeLanguage("en");
});

describe("App", () => {
  it("shows the API version when the API is up", async () => {
    mockFetch({ ok: true, json: () => Promise.resolve({ status: "ok", version: "1.2.3" }) });
    render(<App />);
    expect(await screen.findByText("up (v1.2.3)")).toBeInTheDocument();
  });

  it("reports an unreachable API", async () => {
    mockFetch(new Error("network"));
    render(<App />);
    expect(await screen.findByText("unreachable")).toBeInTheDocument();
  });

  it("switches language to French", async () => {
    mockFetch(new Error("network"));
    render(<App />);
    await act(async () => {
      fireEvent.change(screen.getByRole("combobox"), { target: { value: "fr" } });
    });
    expect(await screen.findByText("injoignable")).toBeInTheDocument();
  });
});

describe("fetchHealth", () => {
  it("throws on HTTP errors", async () => {
    mockFetch({ ok: false, status: 503 });
    await expect(fetchHealth()).rejects.toThrow("HTTP 503");
  });
});
