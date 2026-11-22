import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "../App";
import type { NotificationSettings, StorageSettings } from "../api/types";
import i18n from "../i18n";
import { fakeApi, user } from "../test/fakeApi";

const stored: NotificationSettings = {
  enabled: true,
  host: "smtp.example.com",
  port: 587,
  security: "starttls",
  username: "alerts@example.com",
  password_set: true,
  sender: "carborewind@example.com",
  recipients: ["admin@example.com"],
  events: {
    job_failed: true,
    backup_partial: true,
    restore_failed: true,
    agent_offline: true,
    daily_digest: false,
  },
  digest_hour: 8,
  timezone: "Africa/Algiers",
  language: "fr",
  secret_key_configured: true,
};

const storage: StorageSettings = {
  revision: 2,
  primary_kind: "smb",
  primary_smb: {
    share: "//nas1.lnhc.dz/backups",
    username: "svc",
    domain: "",
    seal: false,
    password_set: true,
  },
  migrate: true,
  copy_02: null,
  agents: [
    {
      agent: "mail",
      path: "/var/lib/carborewind/nas/repository",
      kind: "network",
      fstype: "cifs",
      source: "//nas1.lnhc.dz/backups",
      total_bytes: 1024 ** 4,
      free_bytes: 512 * 1024 ** 3,
      apply: {
        revision: 2,
        ok: false,
        at: "2026-11-19T10:00:00Z",
        error: "mount error(13): Permission denied",
      },
    },
  ],
  applied: true,
  last_replication: null,
  secret_key_configured: true,
};

function open(
  role = user("super_admin"),
  extra: Record<string, unknown> = {},
  path = "/settings",
) {
  let saved: Record<string, unknown> | undefined;
  const calls = fakeApi({
    "GET /api/auth/me": role,
    "GET /api/settings/notifications": stored,
    "PUT /api/settings/notifications": (c: { body: unknown }) => {
      saved = c.body as Record<string, unknown>;
      return {
        body: { ...stored, ...saved, password_set: saved.password !== "" },
      };
    },
    "POST /api/settings/notifications/test": { ok: true, error: null },
    "GET /api/settings/notifications/outbox": [
      {
        id: 2,
        event: "job_failed",
        subject: "[CarboRewind] Failed: backup of a@x",
        status: "failed",
        attempts: 6,
        error: "timed out",
        created_at: "2026-11-18T09:00:00Z",
        sent_at: null,
      },
      {
        id: 1,
        event: "test",
        subject: "[CarboRewind] Digest",
        status: "sent",
        attempts: 1,
        error: null,
        created_at: "2026-11-18T08:00:00Z",
        sent_at: "2026-11-18T08:00:01Z",
      },
    ],
    "GET /api/settings/storage": storage,
    "PUT /api/settings/storage": (c: { body: unknown }) => ({
      body: {
        ...storage,
        revision: 3,
        applied: false,
        copy_02: (c.body as { copy_02: unknown }).copy_02
          ? {
              share: "//nas2/copie02",
              username: "u2",
              domain: "",
              seal: true,
              password_set: true,
            }
          : null,
      },
    }),
    "POST /api/settings/storage/replicate": {
      status: 202,
      body: { job_id: "j1" },
    },
    ...extra,
  });
  window.history.pushState({}, "", path);
  render(<App />);
  return { calls, saved: () => saved };
}

beforeEach(async () => {
  await i18n.changeLanguage("en");
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("settings page", () => {
  it("shows stored settings without the password, and the outbox", async () => {
    open();
    expect(
      await screen.findByDisplayValue("smtp.example.com"),
    ).toBeInTheDocument();
    expect(screen.getByText("stored (encrypted)")).toBeInTheDocument();
    expect(screen.getByLabelText(/Daily digest/)).not.toBeChecked();
    expect(screen.getByLabelText(/Job failed for good/)).toBeChecked();
    expect(await screen.findByText("timed out")).toBeInTheDocument();
    expect(screen.getByText("[CarboRewind] Digest")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Settings" })).toBeInTheDocument();
  });

  it("saves changes, keeping the stored password", async () => {
    const { saved } = open();
    await screen.findByDisplayValue("smtp.example.com");
    fireEvent.change(screen.getByLabelText("Encryption"), {
      target: { value: "ssl" },
    });
    expect(screen.getByLabelText("Port")).toHaveValue(465);
    fireEvent.change(screen.getByLabelText(/Recipients/), {
      target: { value: "a@example.com; b@example.com\nc@example.com" },
    });
    fireEvent.click(screen.getByLabelText(/Daily digest/));
    fireEvent.change(screen.getByLabelText("Digest hour"), {
      target: { value: "7" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(saved()).toBeDefined());
    expect(saved()).toMatchObject({
      security: "ssl",
      port: 465,
      password: null,
      recipients: ["a@example.com", "b@example.com", "c@example.com"],
      digest_hour: 7,
      events: { daily_digest: true },
    });
    expect(saved()).not.toHaveProperty("recipientsText");
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Settings saved.",
    );
  });

  it("changes or clears the password", async () => {
    const { saved } = open();
    await screen.findByDisplayValue("smtp.example.com");
    fireEvent.click(screen.getByRole("button", { name: "Change" }));
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "n3w" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(saved()?.password).toBe("n3w"));
    fireEvent.click(await screen.findByRole("button", { name: "Clear" }));
    expect(screen.getByText("will be cleared")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(saved()?.password).toBe(""));
  });

  it("sends a test e-mail and reports failures", async () => {
    const { calls } = open(user("super_admin"), {
      "POST /api/settings/notifications/test": (c: { body: unknown }) => ({
        body: (c.body as { to?: string }).to
          ? { ok: false, error: "SMTPAuthenticationError: bad credentials" }
          : { ok: true, error: null },
      }),
    });
    await screen.findByDisplayValue("smtp.example.com");
    fireEvent.click(screen.getByRole("button", { name: "Send a test e-mail" }));
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Test e-mail sent.",
    );
    fireEvent.change(screen.getByLabelText("Test recipient (optional)"), {
      target: { value: "me@example.com" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send a test e-mail" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "bad credentials",
    );
    expect(calls.filter((c) => c.method === "POST").map((c) => c.body)).toEqual(
      [{ server: "main" }, { to: "me@example.com", server: "main" }],
    );
  });

  it("reports API errors and a missing secret key", async () => {
    open(user("super_admin"), {
      "GET /api/settings/notifications": {
        ...stored,
        password_set: false,
        secret_key_configured: false,
      },
      "PUT /api/settings/notifications": {
        status: 409,
        body: { detail: "no key" },
      },
      "POST /api/settings/notifications/test": {
        status: 500,
        body: { detail: "down" },
      },
      "GET /api/settings/notifications/outbox": [],
    });
    expect(
      await screen.findByText(/CARBOREWIND_SECRET_KEY is not configured/),
    ).toBeInTheDocument();
    expect(await screen.findByText("No message yet.")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "x" },
    });
    fireEvent.change(screen.getByLabelText("Username"), {
      target: { value: "u" },
    });
    fireEvent.change(screen.getByLabelText("Sender"), {
      target: { value: "s@example.com" },
    });
    fireEvent.change(screen.getByLabelText("Server"), {
      target: { value: "mx" },
    });
    fireEvent.change(screen.getByLabelText("Port"), {
      target: { value: "2525" },
    });
    fireEvent.change(screen.getByLabelText("E-mail language"), {
      target: { value: "en" },
    });
    fireEvent.change(screen.getByLabelText("Time zone"), {
      target: { value: "UTC" },
    });
    fireEvent.click(screen.getByLabelText("Send notifications"));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("no key");
    fireEvent.click(screen.getByRole("button", { name: "Send a test e-mail" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("down");
  });

  it("is reserved to super-administrators", async () => {
    open(user("domain_admin", null));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Super-administrators only.",
    );
    expect(screen.queryByRole("link", { name: "Settings" })).toBeNull();
  });
});

function last(list: HTMLElement[]): HTMLElement {
  const el = list.at(-1);
  if (!el) throw new Error("missing");
  return el;
}

describe("storage settings", () => {
  it("shows the NAS, its status, and saves a copy 02", async () => {
    const { calls } = open();
    fireEvent.click(await screen.findByRole("tab", { name: "Storage" }));
    const section = await screen
      .findByRole("region", { name: "Backup storage" })
      .catch(() => screen.findByText("Backup storage"));
    expect(section).toBeInTheDocument();
    expect(
      await screen.findByDisplayValue("//nas1.lnhc.dz/backups"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("mount error(13): Permission denied"),
    ).toBeInTheDocument();
    expect(screen.getByText("not applied")).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("Enable copy 2"));
    const shares = screen.getAllByLabelText("Share (//server/share/folder)");
    fireEvent.change(last(shares), { target: { value: "//nas2/copie02" } });
    const users = screen.getAllByLabelText("Share user");
    fireEvent.change(last(users), { target: { value: "u2" } });
    const passwords = screen.getAllByLabelText("Share password");
    fireEvent.change(last(passwords), {
      target: { value: "pw2" },
    });
    fireEvent.click(
      last(screen.getAllByLabelText("Require SMB 3 transport encryption")),
    );
    fireEvent.click(screen.getByRole("button", { name: "Save and apply" }));
    expect(
      await screen.findByText(/The agent applies them/),
    ).toBeInTheDocument();
    const put = calls.find(
      (c) => c.method === "PUT" && c.url.endsWith("/storage"),
    );
    expect(put?.body).toEqual({
      primary: {
        kind: "smb",
        smb: {
          share: "//nas1.lnhc.dz/backups",
          username: "svc",
          domain: "",
          seal: false,
          password: null,
        },
        migrate: true,
      },
      copy_02: {
        share: "//nas2/copie02",
        username: "u2",
        domain: "",
        seal: true,
        password: "pw2",
      },
    });
    expect(
      await screen.findByText("Waiting for the agent to apply them…"),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Refresh copy 2 now" }));
    expect(
      await screen.findByText("Copy 2 refresh started."),
    ).toBeInTheDocument();
  });

  it("warns before starting an empty repository on the local disk", async () => {
    const { calls } = open(user("super_admin"), {}, "/settings?tab=storage");
    await screen.findByDisplayValue("//nas1.lnhc.dz/backups");
    fireEvent.click(screen.getByLabelText("Local disk of the Carbonio server"));
    fireEvent.click(screen.getByLabelText(/Copy the current repository/));
    expect(
      screen.getByText(/An empty repository will be created/),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Save and apply" }));
    await waitFor(() =>
      expect(
        calls.find((c) => c.method === "PUT" && c.url.endsWith("/storage"))
          ?.body,
      ).toEqual({
        primary: { kind: "local", smb: null, migrate: false },
        copy_02: null,
      }),
    );
  });
});

describe("agent settings", () => {
  it("shows where they come from and saves them", async () => {
    const { calls } = open(
      user("super_admin"),
      {
        "GET /api/settings/agent": {
          revision: 2,
          max_concurrent_accounts: 2,
          max_bandwidth_mbps: 0,
          max_load_per_cpu: 1.5,
          min_free_gb_local: 20,
          min_free_percent_local: 15,
          min_free_gb_network: 2,
          service_monitor: true,
          service_ports: { mta: 25, amavis: 10024 },
          mail_monitor: true,
          autostart_enabled: false,
          autostart_minutes: 5,
          mail_test_after_start: true,
          mail_test_internal: "",
          mail_test_external: "",
          agents: [
            {
              name: "mx1-mail",
              applied: { revision: 2, at: "2026-11-19T10:00:00Z" },
            },
            {
              name: "mx2",
              applied: { revision: 1, at: "2026-11-18T10:00:00Z" },
            },
          ],
        },
        "PUT /api/settings/agent": (c: { body: unknown }) => ({
          body: { ...(c.body as object), revision: 3, agents: [] },
        }),
      },
      "/settings?tab=agent",
    );
    const form = await screen.findByRole("form", {
      name: "Carbonio server agent",
    });
    expect(
      within(form).getByText(/mx1-mail: settings applied/),
    ).toBeInTheDocument();
    expect(within(form).getByText(/mx2: not applied yet/)).toBeInTheDocument();
    fireEvent.change(within(form).getByLabelText(/at the same time/), {
      target: { value: "3" },
    });
    fireEvent.change(within(form).getByLabelText(/Pause when the load/), {
      target: { value: "4" },
    });
    fireEvent.change(within(form).getByLabelText(/Ports to probe/), {
      target: { value: "mta=25\nAmavis: 10024\n\nlmtp 7025" },
    });
    fireEvent.click(within(form).getByLabelText(/Read the MTA log/));
    const delay = within(form).getByLabelText(/Delay before starting/);
    expect(delay).toBeDisabled();
    fireEvent.click(
      within(form).getByLabelText(/Start Carbonio automatically/),
    );
    fireEvent.change(delay, { target: { value: "10" } });
    fireEvent.change(within(form).getByLabelText(/Internal test address/), {
      target: { value: " h.badi@lnhc.dz " },
    });
    fireEvent.change(within(form).getByLabelText(/External test address/), {
      target: { value: "test@gmail.com" },
    });
    fireEvent.click(within(form).getByRole("button", { name: "Save" }));
    expect(await screen.findByText(/Settings saved/)).toBeInTheDocument();
    expect(calls.find((c) => c.method === "PUT")?.body).toEqual({
      max_concurrent_accounts: 3,
      max_bandwidth_mbps: 0,
      max_load_per_cpu: 4,
      min_free_gb_local: 20,
      min_free_percent_local: 15,
      min_free_gb_network: 2,
      service_monitor: true,
      mail_monitor: false,
      autostart_enabled: true,
      autostart_minutes: 10,
      mail_test_after_start: true,
      mail_test_internal: "h.badi@lnhc.dz",
      mail_test_external: "test@gmail.com",
      service_ports: { mta: 25, amavis: 10024, lmtp: 7025 },
    });
  });

  it("refuses a malformed port line", async () => {
    open(
      user("super_admin"),
      {
        "GET /api/settings/agent": {
          revision: 0,
          max_concurrent_accounts: 2,
          max_bandwidth_mbps: 0,
          max_load_per_cpu: 1.5,
          min_free_gb_local: 20,
          min_free_percent_local: 15,
          min_free_gb_network: 2,
          service_monitor: true,
          service_ports: {},
          mail_monitor: true,
          agents: [],
        },
      },
      "/settings?tab=agent",
    );
    const form = await screen.findByRole("form", {
      name: "Carbonio server agent",
    });
    expect(within(form).getByText(/Never saved here/)).toBeInTheDocument();
    fireEvent.change(within(form).getByLabelText(/Ports to probe/), {
      target: { value: "ldap=abc" },
    });
    fireEvent.click(within(form).getByRole("button", { name: "Save" }));
    expect(await screen.findByText(/invalid port line/)).toBeInTheDocument();
  });
});

describe("fallback SMTP and blacklists", () => {
  it("saves a fallback server and tests it", async () => {
    const { calls } = open(user("super_admin"), {
      "GET /api/settings/notifications": {
        ...stored,
        fallback_enabled: true,
        fallback_host: "relay.example.com",
        fallback_port: 465,
        fallback_security: "ssl",
        fallback_username: "relay",
        fallback_password_set: true,
        fallback_sender: "",
      },
      "POST /api/settings/notifications/test": { ok: true, error: null },
    });
    expect(
      await screen.findByDisplayValue("relay.example.com"),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Port (fallback)")).toHaveValue(465);
    fireEvent.change(screen.getByLabelText("Fallback server"), {
      target: { value: "smtp.isp.example.com" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(calls.find((c) => c.method === "PUT")?.body).toMatchObject({
        fallback_enabled: true,
        fallback_host: "smtp.isp.example.com",
        fallback_port: 465,
        fallback_security: "ssl",
        fallback_password: null,
      }),
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Test the fallback server" }),
    );
    await waitFor(() =>
      expect(
        calls.some(
          (c) =>
            c.url.endsWith("/notifications/test") &&
            (c.body as { server: string }).server === "fallback",
        ),
      ).toBe(true),
    );
  });

  it("sets up the blacklist check and checks now", async () => {
    const status = {
      settings: {
        enabled: false,
        ips: [],
        zones: ["zen.spamhaus.org", "bl.spamcop.net"],
        interval_minutes: 60,
      },
      checked_at: null,
      results: [],
    };
    const { calls } = open(
      user("super_admin"),
      {
        "GET /api/monitoring/blacklist": status,
        "PUT /api/monitoring/blacklist": (c: { body: unknown }) => ({
          body: { ...status, settings: c.body },
        }),
        "POST /api/monitoring/blacklist/check": {
          ...status,
          checked_at: "2026-11-19T10:00:00Z",
          results: [
            {
              ip: "203.0.113.10",
              zone: "zen.spamhaus.org",
              listed: false,
              codes: [],
              error: null,
            },
            {
              ip: "203.0.113.10",
              zone: "bl.spamcop.net",
              listed: true,
              codes: ["127.0.0.2"],
              error: null,
            },
          ],
        },
      },
      "/settings?tab=blacklist",
    );
    const form = await screen.findByRole("form", {
      name: "Blacklists (DNSBL)",
    });
    fireEvent.click(within(form).getByLabelText("Check automatically"));
    fireEvent.change(within(form).getByLabelText(/Public IP addresses/), {
      target: { value: "203.0.113.10\n 198.51.100.7 " },
    });
    fireEvent.click(within(form).getByRole("button", { name: "Save" }));
    expect(
      await within(form).findByText("Settings saved."),
    ).toBeInTheDocument();
    expect(calls.find((c) => c.method === "PUT")?.body).toEqual({
      enabled: true,
      ips: ["203.0.113.10", "198.51.100.7"],
      zones: ["zen.spamhaus.org", "bl.spamcop.net"],
      interval_minutes: 60,
    });
    fireEvent.click(within(form).getByRole("button", { name: "Check now" }));
    const table = await within(form).findByRole("table", {
      name: "Result per list",
    });
    expect(within(table).getByText("listed")).toHaveClass("bad");
    expect(within(table).getByText("clean")).toHaveClass("ok");
  });
});

describe("Telegram and restore tests", () => {
  it("sets up the Telegram bot, finds the group and tests it", async () => {
    let cfg = {
      enabled: false,
      token_set: false,
      chat_ids: [] as string[],
      events: { job_failed: true, migration_done: false },
    };
    const { calls } = open(user("super_admin"), {
      "GET /api/settings/telegram": () => ({ body: cfg }),
      "PUT /api/settings/telegram": (c: { body: unknown }) => {
        const b = c.body as {
          enabled: boolean;
          token: string | null;
          chat_ids: string[];
        };
        cfg = {
          ...cfg,
          enabled: b.enabled,
          chat_ids: b.chat_ids,
          token_set: cfg.token_set || !!b.token,
        };
        return { body: cfg };
      },
      "GET /api/settings/telegram/chats": [
        { id: "-1001", title: "Alertes CarboRewind", type: "group" },
      ],
      "POST /api/settings/telegram/test": { ok: true, error: null },
    });
    const form = await screen.findByRole("form", { name: "Telegram alerts" });
    fireEvent.change(within(form).getByLabelText("Bot token"), {
      target: { value: "123456789:AAAA" },
    });
    fireEvent.click(
      within(form).getByRole("button", { name: "Save Telegram" }),
    );
    await waitFor(() =>
      expect(
        calls.find(
          (c) => c.url.endsWith("/api/settings/telegram") && c.method === "PUT",
        )?.body,
      ).toMatchObject({ token: "123456789:AAAA", enabled: false }),
    );
    const again = await screen.findByRole("form", { name: "Telegram alerts" });
    fireEvent.click(within(again).getByRole("button", { name: "Find groups" }));
    fireEvent.click(
      await within(again).findByRole("button", {
        name: 'Add "Alertes CarboRewind" (-1001)',
      }),
    );
    expect(within(again).getByLabelText(/Groups or people/)).toHaveValue(
      "-1001",
    );
    fireEvent.click(within(again).getByLabelText("Enable Telegram alerts"));
    fireEvent.click(within(again).getByLabelText(/Migration batch finished/));
    fireEvent.click(
      within(again).getByRole("button", { name: "Save Telegram" }),
    );
    await waitFor(() => {
      const puts = calls.filter(
        (c) => c.url.endsWith("/api/settings/telegram") && c.method === "PUT",
      );
      expect(puts.at(-1)?.body).toMatchObject({
        enabled: true,
        token: null,
        chat_ids: ["-1001"],
        events: { job_failed: true, migration_done: true },
      });
    });
    const enabled = await screen.findByRole("form", {
      name: "Telegram alerts",
    });
    fireEvent.click(
      within(enabled).getByRole("button", { name: "Send a test message" }),
    );
    expect(
      await within(enabled).findByText(/Test message sent/),
    ).toBeInTheDocument();
  });

  it("configures the restore test", async () => {
    const tests = {
      settings: {
        enabled: false,
        schedule: "45 3 * * 6",
        timezone: "UTC",
        target_account_id: null,
        messages: 20,
        cleanup: true,
      },
      target_email: null,
      history: [
        {
          job_id: "r1",
          status: "succeeded",
          source: "ana@lnhc.dz",
          seq: 4,
          target: "test@lnhc.dz",
          selected: 20,
          restored: 18,
          failed: 2,
          ok: false,
          cleaned: true,
          error: "2 item(s) could not be restored",
          created_at: "2026-11-21T03:45:00Z",
          finished_at: "2026-11-21T03:47:00Z",
        },
      ],
    };
    const { calls } = open(
      user("super_admin"),
      {
        "GET /api/monitoring/restore-tests": tests,
        "PUT /api/monitoring/restore-tests": (c: { body: unknown }) => ({
          body: { ...tests, settings: c.body },
        }),
        "GET /api/accounts": {
          items: [
            {
              id: "t1",
              email: "test@lnhc.dz",
              domain_id: "d",
              domain: "lnhc.dz",
            },
          ],
          total: 1,
          limit: 20,
          offset: 0,
        },
      },
      "/settings?tab=restoreTest",
    );
    const form = await screen.findByRole("form", {
      name: "Automatic restore test",
    });
    expect(within(form).getByText("failed")).toHaveClass("bad");
    expect(within(form).getByText("18 / 20")).toBeInTheDocument();
    fireEvent.click(within(form).getByLabelText("Test automatically"));
    fireEvent.change(within(form).getByLabelText(/Test mailbox/), {
      target: { value: "test@lnhc.dz" },
    });
    fireEvent.change(within(form).getByLabelText("Messages per test"), {
      target: { value: "10" },
    });
    fireEvent.click(within(form).getByRole("button", { name: "Save" }));
    expect(
      await within(form).findByText("Settings saved."),
    ).toBeInTheDocument();
    expect(calls.find((c) => c.method === "PUT")?.body).toEqual({
      enabled: true,
      schedule: "45 3 * * 6",
      timezone: "UTC",
      target_account_id: "t1",
      messages: 10,
      cleanup: true,
    });
  });
});
