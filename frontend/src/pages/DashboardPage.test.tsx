import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
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
      mail_used_bytes: 2000,
      mail_quota_bytes: 10000,
      mailboxes_nearly_full: 3,
      mailbox_alert_percent: 90,
      storage: [
        {
          agent: "mail",
          path: "/var/lib/carborewind/nas/repository",
          kind: "network",
          fstype: "cifs",
          source: "//nas1/backups",
          total_bytes: 1000,
          free_bytes: 500,
          mail_store: {
            path: "/opt/zextras",
            kind: "local",
            fstype: "ext4",
            source: null,
            total_bytes: 4000,
            free_bytes: 1000,
          },
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
      await screen.findByText("Copy 2 — //nas2/copie02"),
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
    await waitFor(() => expect(screen.getByText(/Copy 2/)).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: "Start backup" })).toBeNull();
  });

  it("warns when Carbonio services are down and shows the archive", async () => {
    open(user("super_admin"), {
      "GET /api/dashboard": {
        ...dashboard,
        services_down: ["amavis"],
        mail_queue_total: 120,
        archived_messages: 3,
        archived_bytes: 3 * 1024 ** 2,
        archived_removed_bytes: 2 * 1024 ** 2,
      },
    });
    expect(
      await screen.findByText(/Carbonio service\(s\) down: amavis/),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Open monitoring" }),
    ).toHaveAttribute("href", "/monitoring");
    expect(
      screen.getByText("archived (3 messages, 2 MB freed)"),
    ).toBeInTheDocument();
  });

  it("warns when a server address is blacklisted", async () => {
    open(user("super_admin"), {
      "GET /api/dashboard": {
        ...dashboard,
        blacklisted: ["203.0.113.10 (bl.spamcop.net)"],
      },
    });
    expect(
      await screen.findByText(
        /Server address\(es\) blacklisted: 203.0.113.10 \(bl.spamcop.net\)/,
      ),
    ).toBeInTheDocument();
  });
});

const security = {
  period: "24h",
  since: "2026-11-19T00:00:00Z",
  totals: { ips: 2, failed: 47, blocked: 1 },
  reported: true,
  ips: [
    {
      ip: "203.0.113.9",
      ptr: "mail-1.mx.spammer.co.uk",
      domain: "spammer.co.uk",
      failed_mailbox: 42,
      failed_smtp: 3,
      suspended: 1,
      last_suspended_at: "2026-11-19T10:00:00Z",
      recently_blocked: true,
      first_seen: "2026-11-19T08:00:00Z",
      last_seen: "2026-11-19T11:00:00Z",
      accounts: ["ana@lnhc.dz"],
    },
  ],
};
const spam = {
  period: "24h",
  since: "2026-11-19T00:00:00Z",
  sources: [
    {
      domain: "spammer.co.uk",
      count: 12,
      ips: [{ ip: "203.0.113.9", ptr: "mail-1.mx.spammer.co.uk", count: 7 }],
      sender_domains: ["deals.example"],
    },
  ],
  sender_domains: [{ domain: "deals.example", count: 5 }],
};

describe("customizable dashboard", () => {
  it("warns while Carbonio blocks an IP", async () => {
    open(user("super_admin"), {
      "GET /api/me/dashboard": { widgets: [] },
      "GET /api/mail/security": {
        ...security,
        ips: [
          { ...security.ips[0], recently_blocked: true, trusted: false },
          {
            ...security.ips[0],
            ip: "198.51.100.1",
            recently_blocked: true,
            trusted: true,
          },
        ],
      },
    });
    expect(
      await screen.findByText(
        /Carbonio is blocking an IP address after failed logins: 203.0.113.9\./,
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "See the blocked IPs" }),
    ).toHaveAttribute("href", "/mail");
  });

  it("points to failed logins when the widget only shows blocked IPs", async () => {
    open(user("super_admin"), {
      "GET /api/me/dashboard": {
        widgets: [
          {
            id: "b",
            type: "blocked_ips",
            size: "full",
            options: { only_blocked: true },
          },
        ],
      },
      "GET /api/mail/security": {
        ...security,
        totals: { ips: 2, failed: 34, blocked: 0 },
        ips: [],
        log_status: "ok",
      },
    });
    expect(
      await screen.findByText(
        /2 addresses failed to log in without being blocked/,
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "See them" })).toHaveAttribute(
      "href",
      "/mail",
    );
  });

  it("shows the HTTP status when an error has no detail", async () => {
    open(user("super_admin"), {
      "GET /api/me/dashboard": {
        widgets: [{ id: "p", type: "policies", size: "full", options: {} }],
      },
      "GET /api/policies": { status: 500, body: {} },
    });
    expect(await screen.findByText(/HTTP 500|status 500/)).toBeInTheDocument();
  });

  it("shows the default widgets, with blocked IPs and spam origin for admins", async () => {
    open(user("super_admin"), {
      "GET /api/me/dashboard": { widgets: null },
      "GET /api/mail/security": security,
      "GET /api/mail/spam-sources": spam,
    });
    const blocked = await screen.findByRole("region", {
      name: "Blocked IPs and failed logins",
    });
    expect(await within(blocked).findByText("203.0.113.9")).toBeInTheDocument();
    expect(within(blocked).getByText("blocked")).toBeInTheDocument();
    const origin = screen.getByRole("region", { name: "Spam origin" });
    expect(
      await within(origin).findByText("spammer.co.uk"),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("region", { name: "Key figures" }),
    ).toBeInTheDocument();
  });

  it("hides the security widgets from viewers", async () => {
    open(user("viewer"), { "GET /api/me/dashboard": { widgets: null } });
    expect(
      await screen.findByRole("region", { name: "Key figures" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Spam origin" })).toBeNull();
  });

  it("adds, edits, moves, removes widgets and saves the layout", async () => {
    let saved: unknown;
    const calls = open(user("super_admin"), {
      "GET /api/me/dashboard": {
        widgets: [
          {
            id: "kpis",
            type: "kpis",
            size: "full",
            options: { items: ["accounts", "failed"] },
          },
          { id: "jobs", type: "jobs", size: "full", options: { count: 5 } },
        ],
      },
      "PUT /api/me/dashboard": (c: { body: unknown }) => {
        saved = c.body;
        return { body: c.body };
      },
    });
    const figures = await screen.findByRole("region", { name: "Key figures" });
    expect(await within(figures).findByText("Accounts")).toBeInTheDocument();
    expect(within(figures).queryByText("Running jobs")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Customize" }));
    // Show one more figure, rename and narrow the jobs widget, move it up.
    fireEvent.click(within(figures).getByLabelText("Running jobs"));
    const jobs = screen.getByRole("region", { name: "Latest jobs" });
    fireEvent.change(within(jobs).getByLabelText("Title"), {
      target: { value: "Échecs" },
    });
    const failures = screen.getByRole("region", { name: "Échecs" });
    fireEvent.change(within(failures).getByLabelText("Filter"), {
      target: { value: "failed" },
    });
    fireEvent.click(
      within(failures).getByRole("button", { name: "Half width" }),
    );
    fireEvent.click(
      within(failures).getByRole("button", { name: "Move Échecs up" }),
    );
    // Add a scheduled-backups widget, then remove it again.
    fireEvent.change(screen.getByLabelText("Add a widget"), {
      target: { value: "policies" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    fireEvent.click(
      await screen.findByRole("button", { name: "Remove Scheduled backups" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(saved).toBeDefined());
    expect(saved).toEqual({
      widgets: [
        {
          id: "jobs",
          type: "jobs",
          size: "half",
          title: "Échecs",
          options: { count: 5, status: "failed" },
        },
        {
          id: "kpis",
          type: "kpis",
          size: "full",
          options: { items: ["accounts", "running", "failed"] },
        },
      ],
    });
    await waitFor(() =>
      expect(calls.some((c) => c.url.includes("status=failed"))).toBe(true),
    );
    expect(screen.queryByRole("button", { name: "Save" })).toBeNull();
  });

  it("resets to the default layout", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    let saved: unknown = "untouched";
    open(user("viewer"), {
      "GET /api/me/dashboard": {
        widgets: [{ id: "jobs", type: "jobs", size: "full", options: {} }],
      },
      "PUT /api/me/dashboard": (c: { body: unknown }) => {
        saved = c.body;
        return { body: c.body };
      },
    });
    fireEvent.click(await screen.findByRole("button", { name: "Customize" }));
    fireEvent.click(screen.getByRole("button", { name: "Default layout" }));
    await waitFor(() => expect(saved).toEqual({ widgets: null }));
    expect(
      await screen.findByRole("region", { name: "Key figures" }),
    ).toBeInTheDocument();
  });
});
