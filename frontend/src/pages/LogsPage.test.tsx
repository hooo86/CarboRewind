import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { App } from "../App";
import i18n from "../i18n";
import { fakeApi, user } from "../test/fakeApi";

function open(role = user("super_admin")) {
  const calls = fakeApi({
    "GET /api/auth/me": role,
    "GET /api/logs/server": {
      process: "api",
      available: true,
      lines: [
        {
          ts: "2026-11-19T10:00:00Z",
          level: "INFO",
          logger: "api",
          msg: "started",
        },
        {
          ts: "2026-11-19T10:00:01Z",
          level: "ERROR",
          logger: "api",
          msg: "boom",
          job: "j1",
        },
      ],
    },
    "POST /api/logs/agent": { status: 202, body: { job_id: "l1" } },
    "GET /api/logs/agent/l1": {
      status: "succeeded",
      error: null,
      text: "postfix/smtpd: connect\npostfix/qmgr: removed",
    },
  });
  window.history.pushState({}, "", "/logs");
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

it("shows the API log, filters it by level and text", async () => {
  open();
  const view = await screen.findByLabelText("Logs", { selector: "pre" });
  await waitFor(() => expect(view.textContent).toContain("started"));
  expect(view.textContent).toContain('boom {"job":"j1"}');
  fireEvent.change(screen.getByLabelText("Level"), {
    target: { value: "ERROR" },
  });
  await waitFor(() => expect(view.textContent).not.toContain("started"));
  fireEvent.change(screen.getByLabelText("Filter"), {
    target: { value: "nothing-matches" },
  });
  expect(view.textContent).toBe("(no line)");
});

it("reads a Carbonio log through the agent", async () => {
  const calls = open();
  await screen.findByLabelText("Logs", { selector: "pre" });
  fireEvent.change(screen.getByLabelText("Log"), { target: { value: "mail" } });
  await waitFor(() =>
    expect(
      screen.getByLabelText("Logs", { selector: "pre" }).textContent,
    ).toContain("postfix/qmgr: removed"),
  );
  const ask = calls.find((c) => c.url === "/api/logs/agent");
  expect(ask?.body).toEqual({ source: "mail", lines: 500 });
});

it("is refused to non super admins", async () => {
  open(user("domain_admin", "d1"));
  expect(await screen.findByRole("alert")).toBeInTheDocument();
  expect(screen.queryByRole("link", { name: /^Logs$/ })).toBeNull();
});
