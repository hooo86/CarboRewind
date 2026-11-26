import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { App } from "../App";
import i18n from "../i18n";
import { fakeApi, job, user } from "../test/fakeApi";

beforeEach(async () => {
  await i18n.changeLanguage("en");
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

it("shows the throughput, sorts and pages the jobs", async () => {
  const calls = fakeApi({
    "GET /api/auth/me": user("super_admin"),
    "GET /api/jobs": {
      items: [job({ throughput_bps: 12_400_000, bytes_done: 5 * 1024 ** 3 })],
      total: 60,
      limit: 50,
      offset: 0,
    },
  });
  window.history.pushState({}, "", "/jobs");
  render(<App />);
  expect(await screen.findByText("12.4 MB/s")).toBeInTheDocument();
  expect(screen.getByText(/GB read/)).toBeInTheDocument();
  expect(screen.getByText("1–50 of 60")).toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: /^Duration/ }));
  await waitFor(() =>
    expect(calls.some((c) => c.url.includes("sort=duration"))).toBe(true),
  );
  fireEvent.click(screen.getByRole("button", { name: "Next" }));
  await waitFor(() =>
    expect(calls.some((c) => c.url.includes("offset=50"))).toBe(true),
  );
});
