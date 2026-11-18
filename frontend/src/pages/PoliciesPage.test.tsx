import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "../App";
import type { Policy, PolicyInput } from "../api/types";
import i18n from "../i18n";
import {
  DOMAIN_ID,
  account,
  dashboard,
  fakeApi,
  page,
  user,
} from "../test/fakeApi";
import { buildSchedule, readSchedule } from "./PoliciesPage";

const OTHER_DOMAIN = "d0000000-0000-0000-0000-000000000002";
const domains = page([
  { id: DOMAIN_ID, name: "example.org", account_count: 2 },
  { id: OTHER_DOMAIN, name: "other.org", account_count: 1 },
]);

function policy(over: Partial<Policy> = {}): Policy {
  return {
    id: "p1",
    name: "Nightly",
    target_type: "domain",
    target_id: DOMAIN_ID,
    target_name: "example.org",
    backup_type: "incremental",
    schedule: "0 1 * * *",
    timezone: "Africa/Algiers",
    retention: { last: 0, daily: 14, weekly: 8, monthly: 12 },
    priority: 100,
    enabled: true,
    last_run_at: "2026-11-18T00:00:00Z",
    next_run_at: new Date(Date.now() + 3_600_000).toISOString(),
    created_at: "2026-11-01T00:00:00Z",
    ...over,
  };
}

function open(
  role = user("super_admin"),
  list: Policy[] = [policy()],
  extra = {},
) {
  let created: PolicyInput | undefined;
  let patched: Partial<PolicyInput> | undefined;
  const calls = fakeApi({
    "GET /api/auth/me": role,
    "GET /api/policies": list,
    "GET /api/domains": domains,
    [`GET /api/domains/${DOMAIN_ID}/accounts`]: page([account]),
    "POST /api/policies": (c: { body: unknown }) => {
      created = c.body as PolicyInput;
      return { status: 201, body: policy({ id: "p2", ...created }) };
    },
    "PATCH /api/policies/p1": (c: { body: unknown }) => {
      patched = c.body as Partial<PolicyInput>;
      return { body: policy(patched) };
    },
    "DELETE /api/policies/p1": { status: 204, body: undefined },
    ...extra,
  });
  window.history.pushState({}, "", "/policies");
  render(<App />);
  return { calls, created: () => created, patched: () => patched };
}

beforeEach(async () => {
  await i18n.changeLanguage("en");
  vi.spyOn(window, "confirm").mockReturnValue(true);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("schedule helpers", () => {
  it("reads and builds daily, weekly and custom schedules", () => {
    expect(readSchedule("0 1 * * *")).toEqual({
      preset: "daily",
      time: "01:00",
      day: 0,
    });
    expect(readSchedule("30 22 * * 7")).toEqual({
      preset: "weekly",
      time: "22:30",
      day: 0,
    });
    expect(readSchedule("*/15 * * * *").preset).toBe("custom");
    expect(buildSchedule("daily", "01:05", 0, "")).toBe("5 1 * * *");
    expect(buildSchedule("weekly", "23:00", 6, "")).toBe("0 23 * * 6");
    expect(buildSchedule("custom", "", 0, " 0 2 1 * * ")).toBe("0 2 1 * *");
  });
});

describe("policies page", () => {
  it("lists policies with a readable schedule and retention", async () => {
    open(user("super_admin"), [
      policy(),
      policy({
        id: "p3",
        name: "Weekly full",
        target_type: "global",
        target_id: null,
        target_name: null,
        backup_type: "full",
        schedule: "30 2 * * 0",
        retention: { last: 0, daily: 0, weekly: 0, monthly: 0 },
        enabled: false,
        next_run_at: null,
      }),
      policy({ id: "p4", name: "Odd", schedule: "*/15 * * * *" }),
    ]);
    expect(await screen.findByText("Nightly")).toBeInTheDocument();
    expect(screen.getByText("every day at 01:00")).toBeInTheDocument();
    expect(screen.getByText("every Sunday at 02:30")).toBeInTheDocument();
    expect(screen.getByText("*/15 * * * *")).toBeInTheDocument();
    expect(screen.getAllByText("14 d, 8 wk, 12 mo").length).toBeGreaterThan(0);
    expect(screen.getByText("keep everything")).toBeInTheDocument();
    expect(screen.getByText("All accounts")).toBeInTheDocument();
    expect(screen.getByText("paused")).toBeInTheDocument();
    expect(screen.getAllByText("enabled").length).toBe(2);
    expect(screen.queryByText("never")).not.toBeInTheDocument();
  });

  it("creates a daily domain policy", async () => {
    const { created } = open();
    fireEvent.click(await screen.findByRole("button", { name: "New policy" }));
    fireEvent.change(screen.getByLabelText("Name"), {
      target: { value: "Nuit" },
    });
    fireEvent.change(screen.getByLabelText("Time"), {
      target: { value: "02:15" },
    });
    fireEvent.change(screen.getByLabelText("Days"), {
      target: { value: "30" },
    });
    fireEvent.change(screen.getByLabelText("Last"), {
      target: { value: "-4" },
    });
    fireEvent.change(screen.getByLabelText("Time zone"), {
      target: { value: "Africa/Algiers" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(created()).toBeDefined());
    expect(created()).toMatchObject({
      name: "Nuit",
      target_type: "domain",
      target_id: DOMAIN_ID,
      schedule: "15 2 * * *",
      timezone: "Africa/Algiers",
      retention: { last: 0, daily: 30, weekly: 8, monthly: 12 },
      enabled: true,
    });
    expect(
      screen.queryByRole("button", { name: "Save" }),
    ).not.toBeInTheDocument();
  });

  it("creates weekly, custom, account and global policies", async () => {
    const { created } = open();
    fireEvent.click(await screen.findByRole("button", { name: "New policy" }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "W" } });
    fireEvent.change(screen.getByLabelText("Frequency"), {
      target: { value: "weekly" },
    });
    fireEvent.change(screen.getByLabelText("Day"), { target: { value: "6" } });
    fireEvent.change(screen.getByLabelText("Backup type"), {
      target: { value: "full" },
    });
    fireEvent.change(screen.getByLabelText("Scope"), {
      target: { value: "account" },
    });
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Account"), {
      target: { value: "bob" },
    });
    fireEvent.click(
      await screen.findByRole("button", { name: "bob@example.org" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(created()).toBeDefined());
    expect(created()).toMatchObject({
      target_type: "account",
      target_id: account.id,
      backup_type: "full",
      schedule: "0 1 * * 6",
    });

    fireEvent.click(await screen.findByRole("button", { name: "New policy" }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "G" } });
    fireEvent.change(screen.getByLabelText("Scope"), {
      target: { value: "global" },
    });
    fireEvent.change(screen.getByLabelText("Frequency"), {
      target: { value: "custom" },
    });
    fireEvent.change(screen.getByLabelText(/^Cron expression/), {
      target: { value: "0 3 * * 1-5" },
    });
    fireEvent.click(screen.getByLabelText("Enabled"));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(created()?.name).toBe("G"));
    expect(created()).toMatchObject({
      target_type: "global",
      target_id: null,
      schedule: "0 3 * * 1-5",
      enabled: false,
    });
  });

  it("edits, pauses and deletes a policy, and shows API errors", async () => {
    const { calls, patched } = open(user("super_admin"), [policy()], {
      "POST /api/policies": {
        status: 409,
        body: { detail: "a policy with this name exists" },
      },
    });
    const row = (await screen.findByText("Nightly")).closest(
      "tr",
    ) as HTMLElement;
    fireEvent.click(within(row).getByRole("button", { name: "Edit" }));
    expect(screen.getByLabelText("Scope")).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Time"), {
      target: { value: "03:00" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(patched()?.schedule).toBe("0 3 * * *"));
    expect(patched()).not.toHaveProperty("target_type");

    fireEvent.click(within(row).getByRole("button", { name: "Pause" }));
    await waitFor(() => expect(patched()).toEqual({ enabled: false }));
    fireEvent.click(
      within(row).getByRole("button", { name: "Delete policy Nightly" }),
    );
    await waitFor(() =>
      expect(calls.some((c) => c.method === "DELETE")).toBe(true),
    );

    fireEvent.click(screen.getByRole("button", { name: "New policy" }));
    fireEvent.change(screen.getByLabelText("Name"), {
      target: { value: "Nightly" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "a policy with this name exists",
    );
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByLabelText("Name")).not.toBeInTheDocument();
  });

  it("reports failures to pause or delete", async () => {
    open(user("super_admin"), [policy()], {
      "PATCH /api/policies/p1": { status: 403, body: { detail: "forbidden" } },
      "DELETE /api/policies/p1": { status: 403, body: { detail: "forbidden" } },
    });
    const row = (await screen.findByText("Nightly")).closest(
      "tr",
    ) as HTMLElement;
    fireEvent.click(within(row).getByRole("button", { name: "Pause" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("forbidden");
    vi.mocked(window.confirm).mockReturnValue(false);
    fireEvent.click(
      within(row).getByRole("button", { name: "Delete policy Nightly" }),
    );
  });

  it("limits domain admins to their domain", async () => {
    open(user("domain_admin", DOMAIN_ID), [
      policy(),
      policy({
        id: "p5",
        name: "Other",
        target_id: OTHER_DOMAIN,
        target_name: "other.org",
      }),
      policy({
        id: "p6",
        name: "Global",
        target_type: "global",
        target_id: null,
      }),
    ]);
    const own = (await screen.findByText("Nightly")).closest(
      "tr",
    ) as HTMLElement;
    expect(
      within(own).getByRole("button", { name: "Edit" }),
    ).toBeInTheDocument();
    const other = screen.getByText("Other").closest("tr") as HTMLElement;
    expect(within(other).queryByRole("button")).not.toBeInTheDocument();
    const glob = screen.getByText("Global").closest("tr") as HTMLElement;
    expect(within(glob).queryByRole("button")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "New policy" }));
    const scope = screen.getByLabelText("Scope");
    expect(within(scope).queryByText("All accounts")).not.toBeInTheDocument();
    expect(
      within(screen.getByLabelText("Domain")).queryByText("other.org"),
    ).toBeNull();
  });

  it("shows 'never' for a policy that has not run yet", async () => {
    open(user("viewer"), [
      policy({
        created_at: "2026-11-18T10:00:00Z",
        last_run_at: "2026-11-18T10:00:00Z",
      }),
    ]);
    expect(await screen.findByText("never")).toBeInTheDocument();
  });

  it("shows an empty state, and viewers cannot create", async () => {
    open(user("viewer"), []);
    expect(
      await screen.findByText(/No policy: no backup is scheduled/),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "New policy" }),
    ).not.toBeInTheDocument();
  });
});

describe("dashboard alerts", () => {
  it("warns about unprotected accounts and missing policies", async () => {
    fakeApi({
      "GET /api/auth/me": user("super_admin"),
      "GET /api/dashboard": dashboard,
      "GET /api/jobs": page([]),
      "GET /api/policies": [policy({ enabled: false })],
    });
    window.history.pushState({}, "", "/");
    render(<App />);
    expect(
      await screen.findByText(
        /2 accounts have no successful backup in the last 24 h/,
      ),
    ).toBeInTheDocument();
    expect(await screen.findByText(/No backup policy/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("link", { name: "Create a policy" }));
    expect(
      await screen.findByRole("heading", { name: "Backup policies" }),
    ).toBeInTheDocument();
  });

  it("summarises purge jobs", async () => {
    fakeApi({
      "GET /api/auth/me": user("viewer"),
      "GET /api/jobs": page([
        {
          id: "j9",
          kind: "purge",
          status: "succeeded",
          account_id: null,
          account_email: null,
          backup_type: null,
          attempts: 1,
          progress: { blobs: 12, manifests: 2, freed_bytes: 2048 },
          error: null,
          created_at: "2026-11-18T09:00:00Z",
          started_at: "2026-11-18T09:00:00Z",
          finished_at: "2026-11-18T09:00:02Z",
        },
      ]),
    });
    window.history.pushState({}, "", "/jobs");
    render(<App />);
    expect(
      await screen.findByText(
        "12 object(s) and 2 snapshot(s) deleted, 2 KB freed",
      ),
    ).toBeInTheDocument();
  });
});
