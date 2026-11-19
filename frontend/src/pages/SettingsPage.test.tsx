import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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
      [{}, { to: "me@example.com" }],
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
