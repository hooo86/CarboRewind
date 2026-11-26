import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { App } from "../App";
import { setCsrfToken } from "../api/client";
import i18n from "../i18n";
import { fakeApi, user } from "../test/fakeApi";

const VIEW = {
  config: { enabled: true, interval_minutes: 15 },
  agents: [
    {
      id: "p1",
      name: "mx1",
      hostname: "mail.lnhc.dz",
      role: "primary",
      status: "active",
      last_heartbeat_at: "2026-11-22T10:00:00Z",
    },
    {
      id: "e1",
      name: "old1",
      hostname: "mail.example.org",
      role: "primary",
      status: "pending",
      last_heartbeat_at: null,
    },
    {
      id: "s1",
      name: "secours",
      hostname: "mail.lnhc.dz",
      role: "standby",
      status: "active",
      last_heartbeat_at: "2026-11-22T10:00:00Z",
    },
  ],
  accounts: 120,
  synced: 118,
  partial: 1,
  failed: 1,
  never: 1,
  behind: 3,
  pending_jobs: 2,
  oldest_waiting_at: "2026-11-22T09:20:00Z",
  lag_seconds: 2400,
  last_applied_at: "2026-11-22T09:58:00Z",
  problems: [
    {
      email: "ben@lnhc.dz",
      status: "failed",
      error: "mail.QUOTA_EXCEEDED",
      applied_at: "2026-11-22T09:50:00Z",
    },
  ],
};

function open(role = user("super_admin"), view = VIEW) {
  const calls = fakeApi({
    "GET /api/auth/me": role,
    "GET /api/standby": view,
    "PUT /api/standby/settings": { enabled: true, interval_minutes: 30 },
    "POST /api/standby/sync-now": { queued: 3 },
    "PUT /api/standby/agents/s1/role": { id: "s1", role: "primary" },
    "POST /api/agents/e1/revoke": { id: "e1" },
    "POST /api/standby/promote": { promoted: "secours", demoted: ["mx1"] },
  });
  window.history.pushState({}, "", "/standby");
  render(<App />);
  return calls;
}

beforeEach(async () => {
  await i18n.changeLanguage("en");
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  setCsrfToken(undefined);
});

it("shows the delay, the problems and changes the settings", async () => {
  const calls = open();
  expect(await screen.findByText("40 minutes")).toBeInTheDocument();
  expect(screen.getByText("118 / 120")).toBeInTheDocument();
  expect(screen.getByText("mail.QUOTA_EXCEEDED")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Synchronise now" }));
  expect(await screen.findByRole("status")).toHaveTextContent(
    "3 replays queued.",
  );
  const form = screen.getByRole("form", { name: "Settings" });
  fireEvent.change(
    within(form).getByLabelText("Maximum delay wanted (minutes)"),
    {
      target: { value: "30" },
    },
  );
  fireEvent.click(within(form).getByRole("button", { name: "Save" }));
  expect(await screen.findByText("Settings saved.")).toBeInTheDocument();
  const put = calls.find((c) => c.url === "/api/standby/settings");
  expect(put?.body).toEqual({ enabled: true, interval_minutes: 30 });
  expect(screen.queryByRole("button", { name: "Make standby" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Make primary" }));
  expect(await screen.findByText("secours: role primary.")).toBeInTheDocument();
  expect(screen.getByText("never enrolled")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Remove" }));
  expect(
    await screen.findByText("Agent old1 removed (revoked)."),
  ).toBeInTheDocument();
});

it("switches to the standby server after typing its name", async () => {
  const calls = open();
  fireEvent.click(
    await screen.findByRole("button", { name: "Switch to secours" }),
  );
  const form = screen.getByRole("form", {
    name: "Switch to the standby server",
  });
  const go = within(form).getByRole("button", { name: "Switch" });
  expect(go).toBeDisabled();
  fireEvent.change(within(form).getByLabelText(/Type the agent's name/), {
    target: { value: "secours" },
  });
  fireEvent.click(go);
  expect(
    await screen.findByText("secours is now the primary server."),
  ).toBeInTheDocument();
  const post = calls.find((c) => c.url === "/api/standby/promote");
  expect(post?.body).toEqual({ agent_id: "s1", confirm: "secours" });
});

it("explains what is missing", async () => {
  open(user("super_admin"), {
    ...VIEW,
    config: { enabled: false, interval_minutes: 15 },
    agents: VIEW.agents.slice(0, 1),
    problems: [],
  });
  expect(
    await screen.findByText(/No agent has the standby role/),
  ).toBeInTheDocument();
  expect(
    screen.getByRole("button", { name: "Synchronise now" }),
  ).toBeDisabled();
  expect(screen.queryByRole("button", { name: /Switch to/ })).toBeNull();
});
