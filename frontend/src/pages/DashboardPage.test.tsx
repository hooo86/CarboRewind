import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "../App";
import i18n from "../i18n";
import {
  DOMAIN_ID,
  dashboard,
  fakeApi,
  job,
  page,
  user,
} from "../test/fakeApi";

function open(role = user("super_admin"), extra = {}) {
  const calls = fakeApi({
    "GET /api/auth/me": role,
    "GET /api/dashboard": {
      ...dashboard,
      storage: [
        {
          agent: "mail",
          path: "/var/lib/carborewind/nas/repository",
          kind: "network",
          fstype: "cifs",
          source: "//nas1/backups",
          total_bytes: 1000,
          free_bytes: 500,
          reported_at: "2026-11-19T10:00:00Z",
          replica: {
            path: "/var/lib/carborewind/copy02/repository",
            kind: "network",
            fstype: "cifs",
            source: "//nas2/copie02",
            total_bytes: 2000,
            free_bytes: 1500,
          },
        },
      ],
    },
    "GET /api/jobs": page([job()]),
    "GET /api/policies": [],
    "GET /api/domains": page([{ id: DOMAIN_ID, name: "lnhc.dz" }]),
    "POST /api/backups/all": {
      status: 202,
      body: { queued: 12, skipped_busy: 2, skipped_deleted: 0 },
    },
    ...extra,
  });
  window.history.pushState({}, "", "/");
  render(<App />);
  return calls;
}

beforeEach(async () => {
  await i18n.changeLanguage("en");
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("dashboard", () => {
  it("shows copy 2 and starts a manual backup of a domain", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const calls = open();
    expect(
      await screen.findByText(/copy 2: \/\/nas2\/copie02/),
    ).toBeInTheDocument();
    fireEvent.change(await screen.findByLabelText("Accounts"), {
      target: { value: DOMAIN_ID },
    });
    fireEvent.change(screen.getByLabelText("Type"), {
      target: { value: "full" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Start backup" }));
    expect(
      await screen.findByText(
        "12 backup(s) started (2 account(s) already running).",
      ),
    ).toBeInTheDocument();
    expect(calls.find((c) => c.url === "/api/backups/all")?.body).toEqual({
      backup_type: "full",
      domain_id: DOMAIN_ID,
    });
  });

  it("asks before starting, and reports errors", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    const calls = open(user("super_admin"), {
      "POST /api/backups/all": { status: 500, body: { detail: "boom" } },
    });
    fireEvent.click(
      await screen.findByRole("button", { name: "Start backup" }),
    );
    expect(calls.some((c) => c.url === "/api/backups/all")).toBe(false);
    confirm.mockReturnValue(true);
    fireEvent.click(screen.getByRole("button", { name: "Start backup" }));
    expect(await screen.findByText("Error: boom")).toBeInTheDocument();
  });

  it("hides the manual backup from viewers", async () => {
    open(user("viewer"));
    await waitFor(() => expect(screen.getByText(/copy 2/)).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: "Start backup" })).toBeNull();
  });
});
