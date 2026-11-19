import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "../App";
import type { ArchivedMessage, ArchivePolicy, ArchiveRun } from "../api/types";
import i18n from "../i18n";
import { DOMAIN_ID, fakeApi, page, user } from "../test/fakeApi";

const message = (over: Partial<ArchivedMessage> = {}): ArchivedMessage => ({
  id: 7,
  account_id: "a1",
  account_email: "ana@lnhc.dz",
  folder_path: "/Inbox",
  subject: "Facture 2024",
  sender: "Bob <bob@example.test>",
  recipients: "ana@lnhc.dz",
  item_date: "2024-03-05T09:00:00Z",
  size: 2048,
  has_attachments: true,
  archived_at: "2026-11-19T01:30:00Z",
  removed_at: "2026-11-19T01:31:00Z",
  ...over,
});

const policy = (over: Partial<ArchivePolicy> = {}): ArchivePolicy => ({
  id: "p1",
  name: "Plus d'un an",
  enabled: true,
  mode: "archive",
  domain_ids: [],
  account_ids: [],
  exclude_account_ids: [],
  older_than_days: 365,
  exclude_folders: [],
  keep_flagged: true,
  keep_unread: false,
  include_admins: false,
  max_per_run: 5000,
  schedule: "30 1 * * *",
  timezone: "UTC",
  retention_days: 0,
  effective_mode: "simulate",
  simulation_valid: false,
  cutoff: "2025-11-19T00:00:00Z",
  criteria_changed_at: "2026-11-19T00:00:00Z",
  simulated_at: null,
  last_run_at: null,
  created_at: "2026-11-18T00:00:00Z",
  archived_count: 12,
  archived_bytes: 4 * 1024 ** 2,
  ...over,
});

const run: ArchiveRun = {
  id: "j9",
  run: "r1",
  mode: "simulate",
  policy: "Plus d'un an",
  account_email: "ana@lnhc.dz",
  status: "succeeded",
  progress: {
    candidates: 120,
    candidate_bytes: 50 * 1024 ** 2,
    truncated: false,
  },
  error: null,
  created_at: "2026-11-19T01:30:00Z",
  finished_at: "2026-11-19T01:31:00Z",
};

function open(role = user("super_admin"), extra = {}) {
  const calls = fakeApi({
    "GET /api/auth/me": role,
    "GET /api/archives/summary": {
      messages: 12,
      bytes: 4 * 1024 ** 2,
      removed_messages: 10,
      removed_bytes: 3 * 1024 ** 2,
      accounts: 2,
      last_archived_at: null,
    },
    "GET /api/archives/messages": page([
      message(),
      message({
        id: 8,
        subject: null,
        removed_at: null,
        has_attachments: false,
      }),
    ]),
    "POST /api/archives/messages/7/open": {
      status: 202,
      body: { job_id: "f1" },
    },
    "GET /api/archives/open/f1": {
      status: "succeeded",
      error: null,
      message: {
        subject: "Facture 2024",
        from: "Bob <bob@example.test>",
        to: "ana@lnhc.dz",
        cc: null,
        date: "Tue, 05 Mar 2024 10:00:00 +0100",
        text: "Bonjour Ana",
        truncated: false,
        html_only: true,
        attachments: [
          { name: "f.pdf", content_type: "application/pdf", size: 6 },
        ],
        size: 2048,
      },
    },
    "POST /api/archives/restore": {
      status: 202,
      body: [{ id: "r1", kind: "restore", status: "queued" }],
    },
    "GET /api/accounts": page([
      {
        id: "b1",
        email: "ben@lnhc.dz",
        domain_id: DOMAIN_ID,
        domain: "lnhc.dz",
      },
    ]),
    "GET /api/archives/policies": [policy()],
    "GET /api/archives/mailboxes": {
      items: [
        {
          account_id: "a1",
          account_email: "ana@lnhc.dz",
          messages: 1200,
          bytes: 3 * 1024 ** 3,
          removed_messages: 1000,
          removed_bytes: 2 * 1024 ** 3,
          legal_hold: true,
          oldest: "2019-01-01T00:00:00Z",
          newest: "2024-12-31T00:00:00Z",
          last_archived_at: "2026-11-19T01:30:00Z",
        },
      ],
      total: 1,
      limit: 50,
      offset: 0,
    },
    "POST /api/archives/accounts/a1/restore": {
      status: 202,
      body: [{ id: "r1", kind: "restore", status: "queued" }],
    },
    "GET /api/archives/runs": page([run]),
    "POST /api/archives/policies/p1/run": {
      status: 202,
      body: { run: "r2", mode: "simulate", jobs: 3 },
    },
    "POST /api/archives/policies": (c: { body: unknown }) => ({
      status: 201,
      body: { ...policy(), ...(c.body as object), id: "p2" },
    }),
    "GET /api/domains": page([
      { id: DOMAIN_ID, name: "lnhc.dz", account_count: 3 },
    ]),
    ...extra,
  });
  window.history.pushState({}, "", "/archives");
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

describe("archives page", () => {
  it("searches, reads a message and restores a selection", async () => {
    const calls = open();
    fireEvent.click(await screen.findByRole("tab", { name: "Search" }));
    const tiles = await screen.findByRole("region", {
      name: "Archive summary",
    });
    expect(within(tiles).getByText("12")).toBeInTheDocument();
    expect(within(tiles).getByText("3 MB")).toBeInTheDocument();
    expect(await screen.findByText("2 messages")).toBeInTheDocument();
    expect(screen.getByText("Removed from the mailbox")).toBeInTheDocument();
    expect(screen.getByText("Still in the mailbox")).toBeInTheDocument();
    expect(screen.getByText("(no subject)")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Subject"), {
      target: { value: "facture" },
    });
    fireEvent.change(screen.getByLabelText("From date"), {
      target: { value: "2024-01-01" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() =>
      expect(
        calls.some(
          (c) =>
            c.url.includes("/api/archives/messages") &&
            c.url.includes("q=facture") &&
            c.url.includes("date_from=2024-01-01T00%3A00%3A00Z"),
        ),
      ).toBe(true),
    );

    fireEvent.click(
      await screen.findByRole("button", { name: "Facture 2024" }),
    );
    const viewer = await screen.findByRole("dialog", {
      name: "Archived message",
    });
    expect(await within(viewer).findByText("Bonjour Ana")).toBeInTheDocument();
    expect(
      within(viewer).getByText(/HTML message shown as plain text/),
    ).toBeInTheDocument();
    expect(within(viewer).getByText(/f\.pdf/)).toBeInTheDocument();
    expect(
      within(viewer).getByRole("link", { name: "Download (.eml)" }),
    ).toHaveAttribute("href", "/api/archives/open/f1/eml");
    fireEvent.click(within(viewer).getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    fireEvent.click(
      screen.getByRole("checkbox", { name: 'Select "Facture 2024"' }),
    );
    fireEvent.change(screen.getByLabelText("Into another mailbox (optional)"), {
      target: { value: "ben@lnhc.dz" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Restore 1 message" }));
    expect(await screen.findByText(/1 message to restore/)).toBeInTheDocument();
    const restore = calls.find((c) => c.url.endsWith("/api/archives/restore"));
    expect(restore?.body).toEqual({
      ids: [7],
      mode: "new_folder",
      target_account_id: "b1",
    });
  });

  it("manages policies: simulation gate, runs, creation", async () => {
    const calls = open();
    fireEvent.click(
      await screen.findByRole("tab", { name: "Archive policies" }),
    );
    expect((await screen.findAllByText("Plus d'un an")).length).toBeGreaterThan(
      0,
    );
    expect(screen.getByText(/Simulation required/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run now" })).toBeDisabled();
    expect(
      await screen.findByRole("table", { name: "Latest runs" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/120/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Simulate" }));
    expect(
      await screen.findByText("3 accounts queued (Simulation)."),
    ).toBeInTheDocument();
    expect(
      calls.find((c) => c.url.endsWith("/api/archives/policies/p1/run"))?.body,
    ).toEqual({ simulate: true });

    fireEvent.click(screen.getByRole("button", { name: "New policy" }));
    const form = await screen.findByRole("form", { name: "Archive policy" });
    fireEvent.change(within(form).getByLabelText("Name"), {
      target: { value: "Deux ans" },
    });
    fireEvent.change(within(form).getByLabelText("Older than (days)"), {
      target: { value: "730" },
    });
    fireEvent.click(await within(form).findByLabelText("lnhc.dz"));
    fireEvent.change(within(form).getByLabelText(/Excluded folders/), {
      target: { value: "/Inbox/Juridique\n\n" },
    });
    fireEvent.click(within(form).getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(
        calls.some(
          (c) =>
            c.method === "POST" && c.url.endsWith("/api/archives/policies"),
        ),
      ).toBe(true),
    );
    const created = calls.find(
      (c) => c.method === "POST" && c.url.endsWith("/api/archives/policies"),
    )?.body as Record<string, unknown>;
    expect(created).toMatchObject({
      name: "Deux ans",
      older_than_days: 730,
      mode: "simulate",
      domain_ids: [DOMAIN_ID],
      exclude_folders: ["/Inbox/Juridique"],
      account_ids: [],
    });
  });

  it("is hidden from viewers and has no policies tab for domain admins", async () => {
    open(user("viewer"));
    await screen.findByRole("link", { name: "Dashboard" });
    expect(
      screen.queryByRole("link", { name: "Archives" }),
    ).not.toBeInTheDocument();
    vi.unstubAllGlobals();
  });

  it("shows only the search to a domain administrator", async () => {
    open(user("domain_admin", DOMAIN_ID));
    fireEvent.click(await screen.findByRole("tab", { name: "Search" }));
    expect(
      await screen.findByRole("link", { name: "Archives" }),
    ).toBeInTheDocument();
    expect(await screen.findByText("2 messages")).toBeInTheDocument();
    expect(
      screen.queryByRole("tab", { name: "Archive policies" }),
    ).not.toBeInTheDocument();
  });

  it("puts a mailbox under legal hold and releases it", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const calls = open(user("super_admin"), {
      "GET /api/archives/legal-holds": [
        {
          account_id: "a1",
          account_email: "ana@lnhc.dz",
          messages: 1520,
          bytes: 3 * 1024 ** 3,
          last_archived_at: "2026-11-19T10:00:00Z",
          pending: true,
        },
      ],
      "POST /api/archives/accounts/b1/legal-hold": {
        status: 202,
        body: { id: "h1", kind: "archive", status: "queued" },
      },
      "DELETE /api/archives/accounts/a1/legal-hold": {
        status: 204,
        body: undefined,
      },
    });
    fireEvent.click(await screen.findByRole("tab", { name: "Legal hold" }));
    const list = await screen.findByRole("table", {
      name: "Mailboxes under legal hold",
    });
    expect(within(list).getByText("ana@lnhc.dz")).toBeInTheDocument();
    expect(within(list).getByText("running")).toBeInTheDocument();
    expect(within(list).getByText("3 GB")).toBeInTheDocument();

    const form = screen.getByRole("form", { name: "Legal hold" });
    fireEvent.change(within(form).getByLabelText("Mailbox address"), {
      target: { value: "ben@lnhc.dz" },
    });
    // Suggestions come from the account list as one types.
    await waitFor(() =>
      expect(
        form.querySelector('datalist option[value="ben@lnhc.dz"]'),
      ).not.toBeNull(),
    );
    fireEvent.change(within(form).getByLabelText(/Reason/), {
      target: { value: "Dossier 2026-17" },
    });
    fireEvent.click(
      within(form).getByRole("button", { name: "Archive the mailbox" }),
    );
    expect(
      await screen.findByText(/Archiving of ben@lnhc.dz started/),
    ).toBeInTheDocument();
    expect(
      calls.find((c) => c.url.endsWith("/api/archives/accounts/b1/legal-hold"))
        ?.body,
    ).toEqual({ note: "Dossier 2026-17" });

    fireEvent.click(
      within(list).getByRole("button", { name: "Release the hold" }),
    );
    expect(await screen.findByText("Hold released.")).toBeInTheDocument();
  });

  it("filters the messages under legal hold", async () => {
    const calls = open();
    fireEvent.click(await screen.findByRole("tab", { name: "Search" }));
    await screen.findByText("2 messages");
    fireEvent.change(screen.getByLabelText("Where"), {
      target: { value: "legal" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() =>
      expect(
        calls.some(
          (c) =>
            c.url.includes("legal_hold=true") &&
            !c.url.includes("removed=legal"),
        ),
      ).toBe(true),
    );
  });

  it("lists the mailboxes and restores a whole archived mailbox", async () => {
    const calls = open();
    const table = await screen.findByRole("table", { name: "By mailbox" });
    expect(within(table).getByText("ana@lnhc.dz")).toBeInTheDocument();
    expect(within(table).getByText("Legal hold")).toBeInTheDocument();
    fireEvent.click(
      within(table).getByRole("button", { name: "Restore the mailbox" }),
    );
    const form = screen.getByRole("form", {
      name: "Restore the archive of ana@lnhc.dz",
    });
    expect(
      within(form).getByRole("button", { name: "Restore 1000 messages" }),
    ).toBeInTheDocument();
    fireEvent.click(within(form).getByLabelText(/Only the messages removed/));
    fireEvent.click(
      within(form).getByRole("button", { name: "Restore 1200 messages" }),
    );
    expect(
      await within(form).findByText(/Restore started \(1 job\)/),
    ).toBeInTheDocument();
    expect(
      calls.find((c) => c.url.endsWith("/api/archives/accounts/a1/restore"))
        ?.body,
    ).toEqual({
      mode: "original",
      only_removed: false,
    });
    fireEvent.click(
      within(table).getByRole("button", { name: "See the messages" }),
    );
    await waitFor(() =>
      expect(
        calls.some(
          (c) =>
            c.url.includes("/api/archives/messages") &&
            c.url.includes("account=ana%40lnhc.dz"),
        ),
      ).toBe(true),
    );
  });
});
