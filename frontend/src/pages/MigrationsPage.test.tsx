import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "../App";
import type {
  ImportReport,
  MigrationBatchDetail,
  MigrationSource,
} from "../api/types";
import i18n from "../i18n";
import { fakeApi, user } from "../test/fakeApi";
import { parseLines } from "./MigrationBatchPage";

function must<T>(value: T | null | undefined): T {
  if (value === null || value === undefined) throw new Error("missing");
  return value;
}

const source: MigrationSource = {
  id: "s1",
  name: "Ancien serveur",
  host: "imap.old.test",
  port: 993,
  security: "ssl",
  verify_tls: true,
  auth_mode: "password",
  master_username: null,
  master_password_set: false,
  master_separator: "*",
  batches: 1,
};

const batch: MigrationBatchDetail = {
  id: "b1",
  name: "Lot 1",
  source_id: "s1",
  source_name: "Ancien serveur",
  auth_mode: "password",
  options: { root: "Import", exclude_folders: ["Trash"] },
  created_at: "2026-11-19T08:00:00Z",
  counts: { mailboxes: 3, matched: 2, succeeded: 1, failed: 0, running: 1 },
  mailboxes: [
    {
      id: "m1",
      source_email: "ana@old.test",
      source_login: null,
      password_set: true,
      target_account_id: "a1",
      target_email: "ana@new.test",
      target_status: "active",
      match: "local_part",
      root: null,
      status: "succeeded",
      dry_run: false,
      progress: null,
      stats: {
        copied: 120,
        failed: 2,
        duplicates: 0,
        total_copied: 120,
        total_bytes: 2048,
        finished_at: "2026-11-19T09:00:00Z",
        plan: { messages: 122, total: 122, folders: 4, at: "x" },
      },
      error: "2 message(s) not copied",
      last_run_at: "2026-11-19T09:00:00Z",
    },
    {
      id: "m2",
      source_email: "ben@old.test",
      source_login: "ben",
      password_set: false,
      target_account_id: "a2",
      target_email: "ben@new.test",
      target_status: "active",
      match: "exact",
      root: null,
      status: "running",
      dry_run: false,
      progress: {
        copied: 10,
        folder: "INBOX",
        folders_done: 0,
        folders_total: 3,
      },
      stats: null,
      error: null,
      last_run_at: null,
    },
    {
      id: "m3",
      source_email: "cy@old.test",
      source_login: null,
      password_set: true,
      target_account_id: null,
      target_email: null,
      target_status: null,
      match: null,
      root: null,
      status: "new",
      dry_run: null,
      progress: null,
      stats: null,
      error: null,
      last_run_at: null,
    },
  ],
};

const report: ImportReport = {
  added: 2,
  updated: 0,
  matched: 1,
  unmatched: [
    { line: 3, source: "cy@old.test", candidates: ["cy@a.test", "cy@b.test"] },
  ],
  errors: [{ line: 4, message: "invalid source address: 'x'" }],
};

function open(path: string, role = user("super_admin")) {
  const calls = fakeApi({
    "GET /api/auth/me": role,
    "GET /api/migrations/sources": [source],
    "POST /api/migrations/sources": (c: { body: unknown }) => ({
      status: 201,
      body: { ...source, ...(c.body as object), id: "s2" },
    }),
    "GET /api/migrations/batches": [batch],
    "POST /api/migrations/batches": { status: 201, body: batch },
    "GET /api/migrations/batches/b1": batch,
    "GET /api/domains": {
      items: [{ id: "d1", name: "new.test" }],
      total: 1,
      limit: 200,
      offset: 0,
    },
    "POST /api/migrations/batches/b1/import": report,
    "POST /api/migrations/batches/b1/mailboxes": report,
    "POST /api/migrations/batches/b1/match": { ...report, errors: [] },
    "POST /api/migrations/batches/b1/start": {
      queued: 1,
      skipped: [{ id: "m3", source: "cy@old.test", reason: "no destination" }],
    },
    "POST /api/migrations/batches/b1/cancel": { cancelled: 1 },
    "PATCH /api/migrations/mailboxes/m3": { ...batch.mailboxes[2] },
    "GET /api/accounts": {
      items: [{ id: "a3", email: "cy@new.test" }],
      total: 1,
      limit: 8,
      offset: 0,
    },
  });
  window.history.pushState({}, "", path);
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

describe("parseLines", () => {
  it("reads source;destination;password per line", () => {
    expect(
      parseLines(
        "ana@old.test\n\n ben@old.test ; ben@new.test;pw \nc@o.t,d@n.t",
      ),
    ).toEqual([
      { source_email: "ana@old.test", target_email: null, password: null },
      {
        source_email: "ben@old.test",
        target_email: "ben@new.test",
        password: "pw",
      },
      { source_email: "c@o.t", target_email: "d@n.t", password: null },
    ]);
  });
});

describe("migrations pages", () => {
  it("lists sources and batches, creates a source", async () => {
    const calls = open("/migrations");
    expect(await screen.findByText("imap.old.test:993")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Lot 1" })).toBeInTheDocument();
    expect(screen.getByText("1 in progress")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Add a server" }));
    fireEvent.change(screen.getByLabelText("Name"), {
      target: { value: "Zimbra" },
    });
    fireEvent.change(screen.getByLabelText("IMAP server"), {
      target: { value: "mail.old.test" },
    });
    fireEvent.change(screen.getByLabelText("Encryption"), {
      target: { value: "starttls" },
    });
    expect(screen.getByLabelText("Port")).toHaveValue(143);
    fireEvent.click(screen.getByLabelText(/Verify the server/));
    expect(
      screen.getByText(/passwords can be intercepted/),
    ).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Authentication"), {
      target: { value: "master_plain" },
    });
    fireEvent.change(screen.getByLabelText("Administrator / master user"), {
      target: { value: "admin" },
    });
    fireEvent.change(screen.getByLabelText("Its password"), {
      target: { value: "s3cret" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(
        calls.find((c) => c.method === "POST" && c.url.endsWith("/sources")),
      ).toBeTruthy(),
    );
    const sent = calls.find(
      (c) => c.method === "POST" && c.url.endsWith("/sources"),
    )?.body;
    expect(sent).toMatchObject({
      name: "Zimbra",
      host: "mail.old.test",
      port: 143,
      security: "starttls",
      verify_tls: false,
      auth_mode: "master_plain",
      master_username: "admin",
      master_password: "s3cret",
    });
  });

  it("is reserved to super administrators", async () => {
    open("/migrations", user("domain_admin", "d1"));
    expect(
      await screen.findByText("Super-administrators only."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Migrations" })).toBeNull();
  });

  it("shows matches, progress and runs actions on a batch", async () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    const calls = open("/migrations/b1");
    expect(
      await screen.findByRole("heading", { name: "Lot 1" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/folder "Import"/)).toBeInTheDocument();
    const rows = screen.getAllByRole("row");
    const ana = must(rows.find((r) => within(r).queryByText("ana@old.test")));
    expect(within(ana).getByText("same name")).toBeInTheDocument();
    expect(within(ana).getByText(/120 new, 2 failed/)).toBeInTheDocument();
    expect(within(ana).getByText(/Test: 122 message/)).toBeInTheDocument();
    const ben = must(rows.find((r) => within(r).queryByText("ben@old.test")));
    expect(within(ben).getByText("missing")).toBeInTheDocument();
    expect(
      within(ben).getByText(/10 message\(s\) copied — folder INBOX/),
    ).toBeInTheDocument();
    const cy = must(rows.find((r) => within(r).queryByText("cy@old.test")));
    expect(within(cy).getByText("to choose")).toBeInTheDocument();

    // Filter: only mailboxes without destination.
    fireEvent.click(
      screen.getByRole("button", { name: "Without destination" }),
    );
    expect(screen.queryByText("ana@old.test")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "All" }));

    // Selection + copy.
    fireEvent.click(screen.getByLabelText("Select cy@old.test"));
    expect(screen.getByText("1 mailbox selected")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Start copy" }));
    expect(
      await screen.findByText(
        /1 copy\(ies\) started. 1 skipped: cy@old.test \(no destination\)/,
      ),
    ).toBeInTheDocument();
    const start = calls.find((c) => c.url.endsWith("/start"));
    expect(start?.body).toEqual({ mailbox_ids: ["m3"], dry_run: false });

    fireEvent.click(screen.getByRole("button", { name: "Test connection" }));
    await waitFor(() =>
      expect(
        calls.filter((c) => c.url.endsWith("/start")).at(-1)?.body,
      ).toEqual({ mailbox_ids: ["m3"], dry_run: true }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    expect(await screen.findByText("1 copy(ies) stopped.")).toBeInTheDocument();
    expect(confirm).not.toHaveBeenCalled();

    // Manual lines, with a destination domain.
    fireEvent.change(screen.getByLabelText("Destination domain"), {
      target: { value: "new.test" },
    });
    fireEvent.change(screen.getByLabelText(/One mailbox per line/), {
      target: { value: "dan@old.test;dan@new.test;pw" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    expect(
      await screen.findByText(
        /2 added, 0 updated, 1 matched, 1 without destination, 1 error/,
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/several candidates: cy@a.test, cy@b.test/),
    ).toBeInTheDocument();
    expect(screen.getByText(/line 4:/)).toBeInTheDocument();
    const added = calls.find((c) => c.url.endsWith("/mailboxes"));
    expect(added?.body).toEqual({
      rows: [
        {
          source_email: "dan@old.test",
          target_email: "dan@new.test",
          password: "pw",
        },
      ],
      target_domain: "new.test",
    });

    // File import sends the raw file.
    const file = new File(["source_email\nx@old.test\n"], "list.csv", {
      type: "text/csv",
    });
    fireEvent.change(screen.getByLabelText("File (.xlsx or .csv)"), {
      target: { files: [file] },
    });
    fireEvent.click(screen.getByRole("button", { name: "Import" }));
    await waitFor(() =>
      expect(calls.find((c) => c.url.includes("/import"))).toBeTruthy(),
    );
    const imported = must(calls.find((c) => c.url.includes("/import")));
    expect(imported.url).toContain("target_domain=new.test");
    expect(imported.body).toBe(file);
    expect(imported.headers["Content-Type"]).toBe("application/octet-stream");

    fireEvent.click(
      screen.getByRole("button", { name: "Run automatic matching again" }),
    );
    await waitFor(() =>
      expect(calls.find((c) => c.url.endsWith("/match"))?.body).toEqual({
        target_domain: "new.test",
        all: false,
      }),
    );

    // Edit the unmatched mailbox: choose its destination.
    const editButtons = screen.getAllByRole("button", { name: "Edit" });
    fireEvent.click(must(editButtons.at(-1)));
    const editor = await screen.findByRole("heading", { name: "cy@old.test" });
    const form = must(editor.closest("form"));
    fireEvent.change(within(form).getByLabelText("Destination (Carbonio)"), {
      target: { value: "cy@new.test" },
    });
    fireEvent.click(within(form).getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(calls.find((c) => c.method === "PATCH")?.body).toEqual({
        source_login: null,
        root: null,
        target_email: "cy@new.test",
      }),
    );
  });

  it("asks for a file before importing", async () => {
    open("/migrations/b1");
    fireEvent.click(await screen.findByRole("button", { name: "Import" }));
    expect(await screen.findByText("Choose a file.")).toBeInTheDocument();
  });
});
