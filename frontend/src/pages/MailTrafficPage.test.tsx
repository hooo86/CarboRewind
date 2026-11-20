import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "../App";
import type { MailOverview } from "../api/types";
import i18n from "../i18n";
import { fakeApi, user } from "../test/fakeApi";

const now = new Date().toISOString();

function first(list: HTMLElement[]): HTMLElement {
  const el = list.at(0);
  if (!el) throw new Error("missing");
  return el;
}
const overview: MailOverview = {
  period: "24h",
  since: now,
  totals: {
    sent_messages: 1234,
    sent_recipients: 5000,
    spam_sent: 6,
    spam_received: 80,
    spam_senders: 12,
  },
  top_senders: [
    {
      email: "ana@lnhc.dz",
      account_id: "a1",
      status: "locked",
      messages: 900,
      recipients: 4000,
      spam_sent: 6,
    },
    {
      email: "ben@lnhc.dz",
      account_id: "a2",
      status: "active",
      messages: 20,
      recipients: 30,
      spam_sent: 0,
    },
  ],
  spam_recipients: [{ email: "ben@lnhc.dz", count: 50 }],
  spam_senders: [
    {
      sender: "promo@spam.example",
      count: 40,
      ips: ["203.0.113.9"],
      last: now,
    },
  ],
  hourly: [
    {
      at: now,
      sent_messages: 900,
      sent_recipients: 4000,
      spam_received: 30,
      spam_sent: 6,
    },
    {
      at: now.replace(/T\d\d/, "T00"),
      sent_messages: 10,
      sent_recipients: 10,
      spam_received: 50,
      spam_sent: 0,
    },
  ],
  alerts: [
    {
      id: "al1",
      account_id: "a1",
      email: "ana@lnhc.dz",
      metric: "sent_messages_hour",
      value: 900,
      threshold: 200,
      window_start: now,
      action: "lock",
      created_at: now,
      acknowledged_at: null,
      account_status: "locked",
    },
  ],
  pending_actions: [
    { id: "p1", email: "ben@lnhc.dz", action: "lock", created_at: now },
  ],
  limits: {
    enabled: true,
    sent_messages_hour: 200,
    sent_recipients_hour: 0,
    sent_messages_day: 1000,
    sent_recipients_day: 0,
    spam_sent_day: 5,
    action: "lock",
    exempt: ["news@lnhc.dz"],
  },
  agents: [{ agent: "mail", log: "/var/log/carbonio.log", reported_at: now }],
};

function open(role = user("super_admin"), extra = {}) {
  const calls = fakeApi({
    "GET /api/auth/me": role,
    "GET /api/mail/overview": overview,
    "PUT /api/mail/limits": (c: { body: unknown }) => ({ body: c.body }),
    "POST /api/mail/alerts/al1/ack": { status: 204, body: undefined },
    "POST /api/mail/accounts/a1/unlock": {
      id: "x1",
      action: "unlock",
      queued: true,
    },
    "POST /api/mail/accounts/a2/lock": {
      id: null,
      action: "lock",
      queued: false,
    },
    ...extra,
  });
  window.history.pushState({}, "", "/mail");
  render(<App />);
  return calls;
}

beforeEach(async () => {
  await i18n.changeLanguage("en");
  vi.spyOn(window, "confirm").mockReturnValue(true);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("mail traffic page", () => {
  it("shows totals, alerts, top senders and spam", async () => {
    open();
    expect(await screen.findByText("1,234")).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Mail traffic" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("img", { name: /Messages sent and spam/ }),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Waiting for the agent: ben@lnhc.dz (lock)"),
    ).toBeInTheDocument();
    const alertRow = first(screen.getAllByText("Messages per hour")).closest(
      "tr",
    );
    expect(alertRow).not.toBeNull();
    expect(
      within(alertRow as HTMLElement).getByText("900 / 200"),
    ).toBeInTheDocument();
    expect(screen.getByText("promo@spam.example")).toBeInTheDocument();
    expect(screen.getByText("203.0.113.9")).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "ana@lnhc.dz" }),
    ).toBeInTheDocument();
  });

  it("acknowledges, unlocks, locks and changes period", async () => {
    const calls = open();
    fireEvent.click(await screen.findByRole("button", { name: "Acknowledge" }));
    expect(await screen.findByText("Alert acknowledged.")).toBeInTheDocument();
    fireEvent.click(first(screen.getAllByRole("button", { name: "Unlock" })));
    expect(
      await screen.findByText(/Unlock of ana@lnhc.dz requested/),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Lock" }));
    expect(await screen.findByText(/already pending/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "7 days" }));
    await waitFor(() =>
      expect(calls.some((c) => c.url.includes("period=7d"))).toBe(true),
    );
  });

  it("edits the limits", async () => {
    const calls = open(user("super_admin"), {
      "GET /api/mail/limits": overview.limits,
    });
    fireEvent.click(
      await screen.findByRole("link", { name: "Sending limits" }),
    );
    fireEvent.change(await screen.findByLabelText("Recipients per hour"), {
      target: { value: "500" },
    });
    fireEvent.click(screen.getByLabelText(/Alert only/));
    fireEvent.change(screen.getByLabelText(/Exempted mailboxes/), {
      target: { value: "news@lnhc.dz\napp@lnhc.dz" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(calls.find((c) => c.method === "PUT")?.body).toEqual({
        ...overview.limits,
        sent_recipients_hour: 500,
        action: "alert",
        exempt: ["news@lnhc.dz", "app@lnhc.dz"],
      }),
    );
  });

  it("warns when nothing comes from the agent, and when limits are off", async () => {
    open(user("super_admin"), {
      "GET /api/mail/overview": {
        ...overview,
        agents: [],
        alerts: [],
        top_senders: [],
        spam_senders: [],
        spam_recipients: [],
        hourly: [],
        pending_actions: [],
        limits: { ...overview.limits, enabled: false },
      },
    });
    expect(
      await screen.findByText(/No statistics received from the agent/),
    ).toBeInTheDocument();
    expect(screen.getByText(/Sending limits are off/)).toBeInTheDocument();
    expect(screen.getByText("No alert.")).toBeInTheDocument();
  });

  it("is reserved to super administrators", async () => {
    open(user("domain_admin", "d1"));
    expect(
      await screen.findByText("Super-administrators only."),
    ).toBeInTheDocument();
  });
});

describe("security and spam origin", () => {
  it("lists blocked IPs and spam networks for the chosen period", async () => {
    const calls = open(user("super_admin"), {
      "GET /api/mail/security": {
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
            suspended: 2,
            last_suspended_at: "2026-11-18T10:00:00Z",
            recently_blocked: false,
            first_seen: "2026-11-18T08:00:00Z",
            last_seen: "2026-11-19T11:00:00Z",
            accounts: ["ana@lnhc.dz", "contact@lnhc.dz"],
          },
          {
            ip: "198.51.100.7",
            ptr: null,
            domain: null,
            failed_mailbox: 0,
            failed_smtp: 2,
            suspended: 0,
            last_suspended_at: null,
            recently_blocked: false,
            first_seen: "2026-11-19T08:00:00Z",
            last_seen: "2026-11-19T09:00:00Z",
            accounts: [],
          },
        ],
      },
      "GET /api/mail/spam-sources": {
        period: "24h",
        since: "2026-11-19T00:00:00Z",
        sources: [
          {
            domain: null,
            count: 4,
            ips: [{ ip: "192.0.2.55", ptr: null, count: 4 }],
            sender_domains: ["nowhere.example"],
          },
        ],
        sender_domains: [{ domain: "nowhere.example", count: 4 }],
      },
    });
    const blocked = await screen.findByRole("region", {
      name: "Failed logins and blocked IPs",
    });
    expect(
      await within(blocked).findByText("mail-1.mx.spammer.co.uk"),
    ).toBeInTheDocument();
    expect(within(blocked).getByText("blocked 2 times")).toBeInTheDocument();
    expect(within(blocked).getByText("not blocked")).toBeInTheDocument();
    expect(
      within(blocked).getByText("42 webmail/IMAP · 3 SMTP"),
    ).toBeInTheDocument();
    expect(
      within(blocked).getByText("ana@lnhc.dz, contact@lnhc.dz"),
    ).toBeInTheDocument();
    expect(
      within(blocked).getByText(
        /2 address\(es\), 47 failed login\(s\), 1 blocked/,
      ),
    ).toBeInTheDocument();
    const origin = screen.getByRole("region", { name: "Spam origin" });
    expect(
      await within(origin).findByText("No reverse name"),
    ).toBeInTheDocument();
    expect(within(origin).getAllByText("nowhere.example")).toHaveLength(2);
    fireEvent.click(screen.getByRole("button", { name: "7 days" }));
    await waitFor(() =>
      expect(
        calls.some(
          (c) =>
            c.url.startsWith("/api/mail/security") &&
            c.url.includes("period=7d"),
        ),
      ).toBe(true),
    );
  });

  it("explains when the agent sends nothing yet", async () => {
    open(user("super_admin"), {
      "GET /api/mail/security": {
        period: "24h",
        since: "2026-11-19T00:00:00Z",
        totals: { ips: 0, failed: 0, blocked: 0 },
        reported: false,
        ips: [],
      },
    });
    expect(
      await screen.findByText(/No data received: update the agent/),
    ).toBeInTheDocument();
    expect(
      screen.getByText("No failed login in the period."),
    ).toBeInTheDocument();
  });
});
