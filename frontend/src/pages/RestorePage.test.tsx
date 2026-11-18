import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "../App";
import { setCsrfToken } from "../api/client";
import type { Job, RestoreRequest } from "../api/types";
import i18n from "../i18n";
import {
  ACCOUNT_ID,
  DOMAIN_ID,
  account,
  fakeApi,
  job,
  page,
  user,
} from "../test/fakeApi";

const SNAP = "s0000000-0000-0000-0000-000000000001";
const OTHER = { ...account, id: "a-other", email: "carol@example.org" };

const detail = {
  id: ACCOUNT_ID,
  email: "bob@example.org",
  status: "active",
  domain_id: DOMAIN_ID,
  domain: "example.org",
  last_seen_at: null,
  snapshot_count: 1,
  last_snapshot: null,
};
const snapshots = page([
  {
    id: SNAP,
    seq: 3,
    backup_type: "full",
    status: "complete",
    taken_at: "2026-11-18T09:00:00Z",
    item_count: 4,
    size_bytes: 4096,
    new_bytes: 4096,
  },
]);
const folders = [
  { id: 2, parent_id: 1, path: "/Inbox", view: "message", item_count: 3 },
  {
    id: 50,
    parent_id: 2,
    path: "/Inbox/Projets",
    view: "message",
    item_count: 1,
  },
  { id: 7, parent_id: 1, path: "/Contacts", view: "contact", item_count: 1 },
];
const items = page([
  {
    id: 301,
    folder_id: 2,
    kind: "message",
    subject: "Facture 1",
    sender: "alice@example.org",
    date: "2026-11-17T09:00:00Z",
    size: 2048,
  },
  {
    id: 302,
    folder_id: 7,
    kind: "contact",
    subject: null,
    sender: null,
    date: null,
    size: 100,
  },
]);

function restoreJob(body: RestoreRequest, over: Partial<Job> = {}): Job {
  return job({
    id: "r1",
    kind: "restore",
    status: "succeeded",
    backup_type: null,
    restore_mode: body.mode,
    dry_run: body.dry_run,
    snapshot_seq: 3,
    progress: {
      dry_run: body.dry_run,
      mode: body.mode,
      root:
        body.mode === "new_folder"
          ? "/CarboRewind 2026-11-18 09.00 UTC #3"
          : null,
      selected: 4,
      restored: 3,
      present: 1,
      unsupported: 0,
      failed_count: 0,
      folders_created: [],
    },
    ...over,
  });
}

function routes(
  role = user("super_admin"),
  extra: Record<string, unknown> = {},
) {
  let last: RestoreRequest | undefined;
  const calls = fakeApi({
    "GET /api/auth/me": role,
    [`GET /api/accounts/${ACCOUNT_ID}`]: detail,
    [`GET /api/accounts/${ACCOUNT_ID}/snapshots`]: snapshots,
    [`GET /api/accounts/${ACCOUNT_ID}/snapshots/${SNAP}/folders`]: folders,
    [`GET /api/accounts/${ACCOUNT_ID}/snapshots/${SNAP}/items`]: items,
    [`GET /api/domains/${DOMAIN_ID}/accounts`]: page([OTHER]),
    "GET /api/jobs": page([]),
    "POST /api/restores": (c: { body: unknown }) => {
      last = c.body as RestoreRequest;
      return {
        status: 202,
        body: restoreJob(last, { status: "queued", progress: null }),
      };
    },
    "GET /api/jobs/r1": () => ({ body: restoreJob(last as RestoreRequest) }),
    ...extra,
  });
  return { calls, posted: () => last };
}

function open() {
  window.history.pushState(
    {},
    "",
    `/accounts/${ACCOUNT_ID}/snapshots/${SNAP}/restore`,
  );
  return render(<App />);
}

beforeEach(async () => {
  await i18n.changeLanguage("en");
  vi.spyOn(window, "confirm").mockReturnValue(true);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  setCsrfToken(undefined);
});

describe("restore page", () => {
  it("is reachable from the snapshot list for restorers only", async () => {
    routes();
    window.history.pushState({}, "", `/accounts/${ACCOUNT_ID}`);
    render(<App />);
    fireEvent.click(
      await screen.findByRole("link", { name: "Restore from snapshot #3" }),
    );
    expect(
      await screen.findByRole("heading", { name: "Restore bob@example.org" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/Snapshot #3 of/)).toBeInTheDocument();
  });

  it("hides restore links and refuses the page to viewers", async () => {
    routes(user("viewer"));
    window.history.pushState({}, "", `/accounts/${ACCOUNT_ID}`);
    const view = render(<App />);
    await screen.findAllByText("4 KB");
    expect(
      screen.queryByRole("link", { name: /Restore from/ }),
    ).not.toBeInTheDocument();
    view.unmount();
    open();
    expect(await screen.findByRole("alert")).toHaveTextContent("not allowed");
  });

  it("simulates a whole-account restore and shows the plan", async () => {
    const { calls, posted } = routes();
    setCsrfToken("csrf-r");
    open();
    fireEvent.click(await screen.findByRole("button", { name: "Simulate" }));
    expect(
      await screen.findByText(/3 items would be restored, 1 already present/),
    ).toBeInTheDocument();
    expect(
      screen.getByText("/CarboRewind 2026-11-18 09.00 UTC #3"),
    ).toBeInTheDocument();
    expect(posted()).toEqual({
      account_id: ACCOUNT_ID,
      snapshot_id: SNAP,
      mode: "new_folder",
      dry_run: true,
    });
    expect(window.confirm).not.toHaveBeenCalled();
    const post = calls.find((c) => c.method === "POST");
    expect(post?.headers["X-CSRF-Token"]).toBe("csrf-r");
  });

  it("restores selected folders into the original folders after confirmation", async () => {
    const { posted } = routes();
    open();
    fireEvent.click(
      await screen.findByLabelText("Folders (with their subfolders)"),
    );
    const restore = screen.getByRole("button", { name: "Restore" });
    expect(restore).toBeDisabled();
    fireEvent.click(await screen.findByRole("checkbox", { name: /Projets/ }));
    expect(screen.getByText("1 selected")).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText(/Into the original folders/));
    fireEvent.click(restore);
    expect(await screen.findByText(/3 items restored/)).toBeInTheDocument();
    expect(posted()).toMatchObject({
      mode: "original",
      dry_run: false,
      folder_ids: [50],
    });
    expect(window.confirm).toHaveBeenCalled();
    // Unticking empties the selection again.
    fireEvent.click(screen.getByRole("checkbox", { name: /Projets/ }));
    expect(screen.getByText("0 selected")).toBeInTheDocument();
  });

  it("does nothing when the confirmation is declined", async () => {
    const { calls } = routes();
    vi.mocked(window.confirm).mockReturnValue(false);
    open();
    fireEvent.click(await screen.findByRole("button", { name: "Restore" }));
    expect(calls.some((c) => c.method === "POST")).toBe(false);
  });

  it("searches and picks items, then restores them into another account", async () => {
    const { calls, posted } = routes();
    open();
    fireEvent.click(await screen.findByLabelText("Specific items"));
    fireEvent.click(
      await screen.findByRole("checkbox", { name: "Select “Facture 1”" }),
    );
    expect(screen.getByText("contacts #302")).toBeInTheDocument();
    expect(screen.getAllByText("/Contacts").length).toBeGreaterThan(0);

    fireEvent.change(screen.getByPlaceholderText("subject or sender"), {
      target: { value: "facture" },
    });
    fireEvent.keyDown(screen.getByPlaceholderText("subject or sender"), {
      key: "Enter",
    });
    await waitFor(() =>
      expect(calls.some((c) => c.url.includes("q=facture"))).toBe(true),
    );
    fireEvent.change(screen.getByLabelText("Folder"), {
      target: { value: "2" },
    });
    await waitFor(() =>
      expect(calls.some((c) => c.url.includes("folder_id=2"))).toBe(true),
    );
    fireEvent.click(screen.getByRole("button", { name: "Search" }));

    fireEvent.click(screen.getByLabelText("Into another account"));
    expect(screen.getByLabelText(/Into the original folders/)).toBeDisabled();
    expect(screen.getByRole("button", { name: "Restore" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Target account"), {
      target: { value: "car" },
    });
    fireEvent.click(
      await screen.findByRole("button", { name: "carol@example.org" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "change" }));
    fireEvent.change(screen.getByLabelText("Target account"), {
      target: { value: "ca" },
    });
    fireEvent.click(
      await screen.findByRole("button", { name: "carol@example.org" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Restore" }));
    await screen.findByText(/3 items restored/);
    expect(posted()).toMatchObject({
      target_account_id: "a-other",
      mode: "new_folder",
      item_ids: [301],
    });
  });

  it("shows API errors and failed restores", async () => {
    routes(user("super_admin"), {
      "POST /api/restores": {
        status: 409,
        body: { detail: "target account is deleted" },
      },
    });
    open();
    fireEvent.click(await screen.findByRole("button", { name: "Simulate" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "target account is deleted",
    );
  });

  it("follows a job until it finishes and reports failures", async () => {
    let polls = 0;
    const body: RestoreRequest = {
      account_id: ACCOUNT_ID,
      snapshot_id: SNAP,
      mode: "new_folder",
      dry_run: false,
    };
    routes(user("restore_operator", DOMAIN_ID), {
      "POST /api/restores": {
        status: 202,
        body: restoreJob(body, { status: "queued", progress: null }),
      },
      "GET /api/jobs/r1": () => {
        polls += 1;
        if (polls === 1) return { status: 500, body: { detail: "boom" } };
        return {
          body: restoreJob(body, {
            error: "1 item(s) could not be restored",
            progress: {
              dry_run: false,
              mode: "new_folder",
              root: null,
              selected: 2,
              restored: 1,
              present: 0,
              unsupported: 1,
              failed_count: 1,
              folders_created: [],
            },
          }),
        };
      },
    });
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      open();
      fireEvent.click(await screen.findByRole("button", { name: "Restore" }));
      await vi.advanceTimersByTimeAsync(6000);
      expect(
        await screen.findByText(
          "1 item restored, 1 not supported (Files), 1 failure",
        ),
      ).toBeInTheDocument();
      expect(
        screen.getByText("1 item(s) could not be restored"),
      ).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("shows restore results in the jobs list", async () => {
    const body: RestoreRequest = {
      account_id: ACCOUNT_ID,
      snapshot_id: SNAP,
      mode: "original",
      dry_run: true,
    };
    routes(user("viewer"), { "GET /api/jobs": page([restoreJob(body)]) });
    window.history.pushState({}, "", "/jobs");
    render(<App />);
    expect(
      await screen.findByText("restore · simulation · #3"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("3 items would be restored, 1 already present"),
    ).toBeInTheDocument();
  });
});
