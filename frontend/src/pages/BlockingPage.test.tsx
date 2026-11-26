import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { App } from "../App";
import i18n from "../i18n";
import { fakeApi, user } from "../test/fakeApi";

beforeEach(async () => {
  await i18n.changeLanguage("en");
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

it("lists the locked accounts and unlocks one", async () => {
  const calls = fakeApi({
    "GET /api/auth/me": user("super_admin"),
    "GET /api/accounts": {
      items: [
        {
          id: "a1",
          email: "ana@lnhc.dz",
          display_name: "Ana",
          status: "locked",
          last_login_at: "2026-11-20T10:00:00Z",
        },
      ],
      total: 1,
      limit: 200,
      offset: 0,
    },
    "POST /api/mail/accounts/a1/unlock": {
      id: "x",
      action: "unlock",
      queued: true,
    },
  });
  window.history.pushState({}, "", "/blocking?tab=accounts");
  render(<App />);
  expect(await screen.findByText("ana@lnhc.dz")).toBeInTheDocument();
  const list = calls.find((c) => c.url.startsWith("/api/accounts?"));
  expect(list?.url).toContain("f=status%3Ain%3Alocked");
  fireEvent.click(screen.getByRole("button", { name: "Unlock" }));
  expect(
    await screen.findByText(
      "ana@lnhc.dz will be unlocked by the agent within a minute.",
    ),
  ).toBeInTheDocument();
  // every blocking feature is a tab of this menu
  for (const name of [
    "Blocked senders",
    "Trusted senders",
    "Spam origin",
    "Blocked IPs",
    "Locked accounts",
    "Sending limits",
    "Blacklists (DNSBL)",
  ])
    expect(screen.getByRole("tab", { name })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Blocking" })).toHaveAttribute(
    "href",
    "/blocking",
  );
});

it("adds a trusted sender", async () => {
  const list = {
    revision: 1,
    entries: [
      {
        value: "@partner.example",
        kind: "domain",
        by: "admin",
        at: "2026-11-26T10:00:00Z",
        reason: "supplier",
      },
    ],
    agents: [],
  };
  const calls = fakeApi({
    "GET /api/auth/me": user("super_admin"),
    "GET /api/mail/trustlist": { ...list, revision: 0, entries: [] },
    "POST /api/mail/trustlist": list,
  });
  window.history.pushState({}, "", "/blocking?tab=trusted");
  render(<App />);
  expect(await screen.findByText("No trusted sender.")).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("Address or domain"), {
    target: { value: "partner.example" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Trust" }));
  expect(
    await screen.findByText(/partner.example added to the trusted senders/),
  ).toBeInTheDocument();
  const post = calls.find((c) => c.method === "POST");
  expect(post?.url).toBe("/api/mail/trustlist");
});

it("is reserved to super administrators", async () => {
  fakeApi({ "GET /api/auth/me": user("viewer") });
  window.history.pushState({}, "", "/blocking");
  render(<App />);
  expect(await screen.findByRole("alert")).toBeInTheDocument();
});
