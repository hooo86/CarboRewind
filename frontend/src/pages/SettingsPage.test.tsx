import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "../App";
import type { NotificationSettings } from "../api/types";
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

function open(role = user("super_admin"), extra: Record<string, unknown> = {}) {
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
    ...extra,
  });
  window.history.pushState({}, "", "/settings");
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
