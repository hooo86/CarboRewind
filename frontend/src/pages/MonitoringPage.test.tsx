import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "../App";
import type { MonitoringOverview } from "../api/types";
import i18n from "../i18n";
import { fakeApi, user } from "../test/fakeApi";

const GIB = 1024 ** 3;
const now = Date.now();
const iso = (ms: number) => new Date(ms).toISOString();

const overview: MonitoringOverview = {
  settings: {
    stale_hours: 24,
    replica_max_lag_hours: 24,
    integrity_enabled: true,
    integrity_cron: "30 3 * * 0",
    integrity_sample: 0.1,
    integrity_timezone: "UTC",
    mailbox_alert_percent: 90,
    mailbox_alert_gb: 0,
    service_down_reports: 2,
    mail_queue_alert: 200,
  },
  storage: [
    {
      agent: "mail",
      target: "primary",
      total_bytes: 100 * GIB,
      free_bytes: 20 * GIB,
      used_bytes: 80 * GIB,
      days_until_full: 8.4,
      growth_per_day: 2 * GIB,
      points: [
        { at: iso(now - 86400000), used_bytes: 78 * GIB },
        { at: iso(now), used_bytes: 80 * GIB },
      ],
    },
    {
      agent: "mail",
      target: "replica",
      total_bytes: 200 * GIB,
      free_bytes: 150 * GIB,
      used_bytes: 50 * GIB,
      days_until_full: null,
      growth_per_day: null,
      points: [{ at: iso(now), used_bytes: 50 * GIB }],
    },
  ],
  at_risk: {
    never: {
      count: 2,
      filters: ["last_backup_at:not_set"],
      accounts: [
        { id: "a1", email: "new1@lnhc.dz", last_backup_at: null },
        { id: "a2", email: "new2@lnhc.dz", last_backup_at: null },
      ],
    },
    stale: { count: 0, filters: ["last_backup_at:older_days:1"], accounts: [] },
    failed: {
      count: 1,
      filters: [],
      accounts: [{ id: "a3", email: "x@lnhc.dz", last_backup_at: null }],
    },
    partial: {
      count: 0,
      filters: ["last_backup_status:in:partial"],
      accounts: [],
    },
    no_policy: { count: 0, filters: ["has_policy:is:false"], accounts: [] },
  },
  replica: {
    last_success_at: iso(now - 3 * 86400000),
    behind: true,
    lag_seconds: 3 * 86400,
    max_lag_hours: 24,
    late: true,
    last_status: "failed",
    last_error: "copy 02 is not mounted",
  },
  integrity: {
    primary: {
      job_id: "j1",
      status: "succeeded",
      created_at: iso(now),
      finished_at: iso(now),
      sample: 0.1,
      progress: {
        checked: 90,
        corrupted_count: 1,
        manifests_checked: 10,
        manifests_corrupted: [],
      },
      error: null,
    },
    replica: null,
  },
  next_integrity_at: iso(now + 86400000),
  jobs_failed_24h: { backup: 2, replicate: 1 },
  agents: [
    {
      name: "mail",
      version: "0.1.0",
      last_heartbeat_at: iso(now),
      cert_not_after: iso(now + 10 * 86400000),
      mail_reported_at: iso(now),
    },
  ],
  metrics_token_configured: true,
};

function open(role = user("super_admin"), extra = {}) {
  const calls = fakeApi({
    "GET /api/auth/me": role,
    "GET /api/monitoring/overview": overview,
    "PUT /api/monitoring/settings": (c: { body: unknown }) => ({
      body: c.body,
    }),
    "POST /api/monitoring/verify": (c: { body: unknown }) => ({
      status: 202,
      body:
        (c.body as { target: string }).target === "replica"
          ? { queued: false, job_id: null }
          : { queued: true, job_id: "v1" },
    }),
    ...extra,
  });
  window.history.pushState({}, "", "/monitoring");
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

describe("monitoring page", () => {
  it("summarises health, storage forecast, risks, copy 2 and integrity", async () => {
    open();
    const tiles = await screen.findByRole("region", { name: "Overall health" });
    expect(screen.getByRole("link", { name: "Monitoring" })).toHaveAttribute(
      "href",
      "/monitoring",
    );
    expect(within(tiles).getByText("Days before full")).toBeInTheDocument();
    expect(within(tiles).getByText("3")).toBeInTheDocument(); // failed jobs
    expect(within(tiles).getByText("3 d")).toBeInTheDocument(); // copy 2 lag
    expect(screen.getByText(/full in about 8 days/)).toBeInTheDocument();
    expect(screen.getByText(/no growth measured/)).toBeInTheDocument();
    expect(screen.getByText(/Not enough measurements/)).toBeInTheDocument();
    expect(screen.getByRole("img", { name: /Copy 1/ })).toBeInTheDocument();
    const never = screen.getByRole("link", { name: "2" });
    expect(never).toHaveAttribute(
      "href",
      "/accounts?f=last_backup_at%3Anot_set",
    );
    expect(
      screen.getByRole("link", { name: "new1@lnhc.dz" }),
    ).toBeInTheDocument();
    expect(screen.getByText("late (> 24 h)")).toBeInTheDocument();
    expect(screen.getByText("copy 02 is not mounted")).toBeInTheDocument();
    expect(screen.getByText("1 corrupted")).toBeInTheDocument();
    expect(screen.getByText(/never checked/)).toBeInTheDocument();
    expect(screen.getByText(/Prometheus metrics: https/)).toBeInTheDocument();
  });

  it("runs checks and edits the settings", async () => {
    const calls = open();
    fireEvent.click(
      await screen.findByRole("button", { name: "Check copy 1 now (full)" }),
    );
    expect(await screen.findByText("Check started.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Check copy 2 now" }));
    expect(await screen.findByText(/already pending/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("link", { name: "Monitoring settings" }));
    fireEvent.change(
      await screen.findByLabelText(/Share of the files checked/),
      {
        target: { value: "25" },
      },
    );
    fireEvent.change(screen.getByLabelText(/Account at risk without backup/), {
      target: { value: "48" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(calls.find((c) => c.method === "PUT")?.body).toEqual({
        ...overview.settings,
        integrity_sample: 0.25,
        stale_hours: 48,
      }),
    );
  });

  it("handles an empty install and errors", async () => {
    open(user("super_admin"), {
      "GET /api/monitoring/overview": {
        ...overview,
        storage: [],
        replica: null,
        integrity: { primary: null, replica: null },
        jobs_failed_24h: {},
        next_integrity_at: null,
        metrics_token_configured: false,
        agents: [
          {
            name: "mail",
            version: null,
            last_heartbeat_at: null,
            cert_not_after: null,
            mail_reported_at: null,
          },
        ],
      },
      "POST /api/monitoring/verify": { status: 500, body: { detail: "boom" } },
    });
    expect(await screen.findByText(/No space measurement/)).toBeInTheDocument();
    expect(screen.getByText(/No copy 2 configured/)).toBeInTheDocument();
    expect(screen.getByText("No scheduled check.")).toBeInTheDocument();
    expect(screen.getByText(/metrics not exposed/)).toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", { name: "Check copy 1 now (full)" }),
    );
    expect(await screen.findByText("Error: boom")).toBeInTheDocument();
  });

  it("is reserved to super administrators", async () => {
    open(user("viewer"));
    expect(
      await screen.findByText("Super-administrators only."),
    ).toBeInTheDocument();
  });

  it("shows the Carbonio services, the stuck queue and the alert settings", async () => {
    const calls = open(user("super_admin"), {
      "GET /api/monitoring/overview": {
        ...overview,
        services: [
          {
            agent: "mail",
            checked_at: iso(now),
            stale: false,
            services: [
              {
                name: "amavis",
                ok: false,
                source: "carbonio",
                since: iso(now - 600000),
              },
              { name: "port:amavis", ok: false, source: "port", port: 10024 },
              { name: "mta", ok: true, source: "carbonio" },
              { name: "stats", ok: null, source: "carbonio" },
              {
                name: "mta:antivirus",
                ok: false,
                source: "mta",
                detail: "status=deferred (mail transport unavailable)",
              },
              {
                name: "guard:master.cf",
                ok: true,
                source: "guard",
                detail: "amavis: 3 · dpkg-divert: oui",
              },
            ],
            down: ["amavis", "port:amavis", "mta:antivirus"],
            queue: { deferred: 340, active: 2 },
            queue_total: 342,
            errors: [],
          },
        ],
      },
    });
    expect(
      await screen.findByText(
        /Carbonio service\(s\) down: amavis, port:amavis/,
      ),
    ).toBeInTheDocument();
    const tiles = screen.getByRole("region", { name: "Overall health" });
    expect(
      within(tiles).getByText("Carbonio services down"),
    ).toBeInTheDocument();
    expect(screen.getByText("amavis — stopped")).toBeInTheDocument();
    expect(
      screen.getByText("port amavis (10024) — stopped"),
    ).toBeInTheDocument();
    expect(screen.getByText("mta — running")).toBeInTheDocument();
    expect(screen.getByText("stats — unknown")).toBeInTheDocument();
    expect(
      screen.getByText(
        /Mail delivery is failing \(Postfix: antivirus circuit\)/,
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Postfix: antivirus circuit — failing"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("status=deferred (mail transport unavailable)"),
    ).toBeInTheDocument();
    expect(screen.getByText("master.cf guard — OK")).toBeInTheDocument();
    expect(
      screen.getByText("amavis: 3 · dpkg-divert: oui"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("queue: 342 messages (deferred 340, active 2)"),
    ).toHaveClass("bad-text");
    fireEvent.click(screen.getByRole("link", { name: "Monitoring settings" }));
    fireEvent.change(
      await screen.findByLabelText("Mail queue alert from (messages, 0 = off)"),
      {
        target: { value: "50" },
      },
    );
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(
        calls.find((c) => c.method === "PUT")?.body as Record<string, unknown>,
      ).toMatchObject({ mail_queue_alert: 50, service_down_reports: 2 }),
    );
  });

  it("lists the queued messages and sends queue actions", async () => {
    const queue = [
      {
        agent_id: "ag-1",
        agent: "mx1-mail",
        checked_at: iso(now),
        queue: { deferred: 2, active: 0 },
        queue_total: 2,
        details: [
          {
            server: "mail.lnhc.dz",
            queue: "deferred",
            total: 2,
            items: [
              {
                id: "6AE741413C3",
                from: "zextras@lnhc.dz",
                to: "a@example.com,b@example.com",
                time: Math.round((now - 3600000) / 1000),
                reason: "mail transport unavailable",
                origip: "127.0.0.1",
              },
              {
                id: "C0FAB141739",
                from: "",
                to: "c@example.com",
                time: null,
                reason: "mail transport unavailable",
                origip: "",
              },
            ],
            reasons: [{ reason: "mail transport unavailable", count: 2 }],
          },
        ],
        pending: [],
        results: [
          {
            id: "r1",
            op: "flush",
            queue: null,
            ids: [],
            by: "admin2",
            at: iso(now),
            done_at: iso(now),
            error: null,
          },
        ],
      },
    ];
    const calls = open(user("super_admin"), {
      "GET /api/monitoring/queue": queue,
      "POST /api/monitoring/queue/actions": { status: 202, body: { id: "q1" } },
    });
    expect(await screen.findByText("Mail queue")).toBeInTheDocument();
    expect(screen.getByText("Deferred — 2 messages")).toBeInTheDocument();
    expect(
      screen.getByText("2 × mail transport unavailable"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("a@example.com, b@example.com"),
    ).toBeInTheDocument();
    expect(screen.getByText("<>")).toBeInTheDocument();
    const retry = screen.getByRole("button", { name: "Retry" });
    expect(retry).toBeDisabled();
    fireEvent.click(screen.getByLabelText("Select 6AE741413C3"));
    fireEvent.click(retry);
    await screen.findByText(
      "Action sent to the agent: applied within a minute.",
    );
    const sent = calls.filter((c) => c.url === "/api/monitoring/queue/actions");
    expect(sent[0]?.body).toEqual({
      agent_id: "ag-1",
      op: "requeue",
      queue: "deferred",
      ids: ["6AE741413C3"],
      server: "mail.lnhc.dz",
    });

    // deleting asks to type the number of messages
    fireEvent.click(screen.getByLabelText("Select all"));
    const prompt = vi.spyOn(window, "prompt").mockReturnValueOnce("1");
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    expect(
      await screen.findByText(
        "Deletion cancelled: the number typed does not match.",
      ),
    ).toBeInTheDocument();
    prompt.mockReturnValueOnce("2");
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    await waitFor(() =>
      expect(
        calls.filter((c) => c.url === "/api/monitoring/queue/actions"),
      ).toHaveLength(2),
    );
    const del = calls.filter(
      (c) => c.url === "/api/monitoring/queue/actions",
    )[1];
    expect(del?.body).toMatchObject({ op: "delete", confirm: 2 });
    fireEvent.click(screen.getByRole("button", { name: "Retry all now" }));
    await waitFor(() =>
      expect(
        calls.filter((c) => c.url === "/api/monitoring/queue/actions"),
      ).toHaveLength(3),
    );
    prompt.mockRestore();
  });
});
