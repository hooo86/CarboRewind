import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "../App";
import { setCsrfToken } from "../api/client";
import type { AccountItem } from "../api/types";
import i18n from "../i18n";
import { DOMAIN_ID, fakeApi, page, user } from "../test/fakeApi";
import { filterLabel, groupFilters, groupLabel } from "./AccountsPage";

const OTHER = "d0000000-0000-0000-0000-000000000002";

function item(over: Partial<AccountItem> = {}): AccountItem {
  return {
    id: "a1",
    email: "ana@example.org",
    domain_id: DOMAIN_ID,
    domain: "example.org",
    status: "active",
    display_name: "Ana Lopez",
    first_name: "Ana",
    last_name: "Lopez",
    aliases: ["direction@example.org"],
    cos: "vip",
    quota_bytes: 1024 * 1024 * 1024,
    used_bytes: 950 * 1024 * 1024,
    usage_pct: 92.8,
    is_admin: true,
    last_login_at: "2026-11-17T09:00:00Z",
    carbonio_created_at: "2024-01-15T00:00:00Z",
    last_seen_at: "2026-11-18T09:00:00Z",
    last_backup_at: new Date(Date.now() - 3_600_000).toISOString(),
    last_backup_status: "complete",
    last_backup_type: "full",
    snapshot_count: 4,
    stored_bytes: 2048,
    policies: ["Nightly"],
    ...over,
  };
}

const ben = item({
  id: "a2",
  email: "ben@example.org",
  display_name: null,
  aliases: [],
  quota_bytes: 0,
  used_bytes: 10,
  usage_pct: 85,
  is_admin: false,
  last_backup_at: null,
  last_backup_status: null,
  last_backup_type: null,
  snapshot_count: 0,
  stored_bytes: null,
  policies: [],
});
const cy = item({
  id: "a3",
  email: "cy@other.org",
  display_name: "Cy",
  domain_id: OTHER,
  domain: "other.org",
});

const facets = {
  domains: [
    { id: DOMAIN_ID, name: "example.org" },
    { id: OTHER, name: "other.org" },
  ],
  cos: ["default", "vip"],
  fields: {
    email: "text",
    domain: "text",
    status: "choice",
    cos: "choice",
    is_admin: "boolean",
    usage_pct: "number",
    last_login_at: "date",
    has_policy: "boolean",
    policy: "text",
  },
  groupable: ["domain", "status", "is_admin", "last_login_at"],
};

function open(role = user("super_admin"), rows = [item(), ben], extra = {}) {
  const calls = fakeApi({
    "GET /api/auth/me": role,
    "GET /api/accounts/facets": facets,
    "GET /api/accounts": (c: { url: string }) => {
      const params = new URL(c.url, "https://x").searchParams;
      const offset = Number(params.get("offset") ?? 0);
      return {
        body: { items: rows, total: rows.length + offset, limit: 50, offset },
      };
    },
    "GET /api/policies": [],
    "GET /api/domains": page([]),
    "POST /api/backups/bulk": {
      status: 202,
      body: { queued: 1, skipped_busy: 1, skipped_deleted: 0 },
    },
    ...extra,
  });
  window.history.pushState({}, "", "/accounts");
  render(<App />);
  const lastQuery = () => {
    const urls = calls.filter((c) => c.url.startsWith("/api/accounts?"));
    return new URL(urls[urls.length - 1]?.url ?? "/", "https://x").searchParams;
  };
  return { calls, lastQuery };
}

beforeEach(async () => {
  await i18n.changeLanguage("en");
  localStorage.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  setCsrfToken(undefined);
});

describe("account list", () => {
  it("shows the default columns with formatted values", async () => {
    open();
    expect(
      await screen.findByRole("link", { name: "ana@example.org" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Ana Lopez")).toBeInTheDocument();
    expect(screen.getByText("92.8 %")).toHaveClass("bad-text");
    expect(screen.getByText("85 %")).toHaveClass("warn-text");
    expect(screen.getByText("Nightly")).toBeInTheDocument();
    expect(screen.getByText("none")).toBeInTheDocument(); // ben has no policy
    expect(screen.getByText("never")).toBeInTheDocument();
    expect(
      screen.queryByRole("columnheader", { name: "Class of service" }),
    ).toBeNull();
  });

  it("lets the user choose columns and remembers them", async () => {
    open();
    await screen.findByText("Ana Lopez");
    fireEvent.click(screen.getByRole("button", { name: "Columns ▾" }));
    const menu = screen.getByRole("dialog", { name: "Columns" });
    for (const label of [
      "Class of service",
      "Aliases",
      "Administrator",
      "Quota",
      "Used space",
      "Last login",
      "Created",
      "Seen by inventory",
      "Last backup type",
      "Snapshots",
      "Backed-up size",
      "Domain",
      "First name",
      "Last name",
    ]) {
      fireEvent.click(within(menu).getByLabelText(label));
    }
    fireEvent.click(within(menu).getByLabelText("Display name")); // hide one
    expect(
      screen.getByRole("columnheader", { name: "Class of service" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("columnheader", { name: "Display name" }),
    ).toBeNull();
    expect(screen.getByText("direction@example.org")).toBeInTheDocument();
    expect(screen.getAllByText("1 GB").length).toBeGreaterThan(0);
    expect(screen.getByText("unlimited")).toBeInTheDocument();
    expect(screen.getByText("2 KB")).toBeInTheDocument();
    expect(screen.getAllByText("yes").length).toBe(1);
    const saved = JSON.parse(
      localStorage.getItem("carborewind.accounts.columns") ?? "[]",
    );
    expect(saved).toContain("cos");
    expect(saved).not.toContain("display_name");
    fireEvent.click(
      within(menu).getByRole("button", { name: "Default columns" }),
    );
    expect(
      screen.queryByRole("columnheader", { name: "Class of service" }),
    ).toBeNull();
  });

  it("restores saved columns and ignores corrupted settings", async () => {
    localStorage.setItem(
      "carborewind.accounts.columns",
      JSON.stringify(["email", "cos"]),
    );
    open();
    expect(
      await screen.findByRole("columnheader", { name: "Class of service" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("columnheader", { name: "Display name" }),
    ).toBeNull();
  });

  it("filters with presets, custom filters and search, as removable chips", async () => {
    const { lastQuery } = open();
    await screen.findByText("Ana Lopez");
    fireEvent.click(screen.getByRole("button", { name: "Filters ▾" }));
    fireEvent.click(screen.getByLabelText("Never backed up"));
    await waitFor(() =>
      expect(lastQuery().getAll("f")).toEqual(["last_backup_at:not_set"]),
    );
    expect(screen.getByLabelText("Never backed up")).toBeChecked();

    // Custom filter on a hidden field: usage ≥ 80.
    fireEvent.change(screen.getByLabelText("Field"), {
      target: { value: "usage_pct" },
    });
    fireEvent.change(screen.getByLabelText("Condition"), {
      target: { value: "ge" },
    });
    fireEvent.change(screen.getByLabelText("Value"), {
      target: { value: "80" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    await waitFor(() =>
      expect(lastQuery().getAll("f")).toEqual([
        "last_backup_at:not_set",
        "usage_pct:ge:80",
      ]),
    );
    expect(screen.getByText("Never backed up")).toBeInTheDocument(); // chip
    expect(screen.getByText("Quota used ≥ 80 %")).toBeInTheDocument(); // same as the preset

    // Search box.
    fireEvent.change(screen.getByRole("searchbox", { name: "Search" }), {
      target: { value: " lopez " },
    });
    fireEvent.submit(screen.getByRole("search"));
    await waitFor(() => expect(lastQuery().get("q")).toBe("lopez"));

    // Remove a chip.
    fireEvent.click(
      screen.getByRole("button", { name: "Remove filter Never backed up" }),
    );
    await waitFor(() =>
      expect(lastQuery().getAll("f")).toEqual(["usage_pct:ge:80"]),
    );
  });

  it("builds choice, boolean, date and domain filters, and clears them", async () => {
    const { lastQuery } = open();
    await screen.findByText("Ana Lopez");
    fireEvent.click(screen.getByRole("button", { name: "Filters ▾" }));
    fireEvent.change(screen.getByLabelText("Field"), {
      target: { value: "cos" },
    });
    expect(screen.getByRole("button", { name: "Apply" })).toBeDisabled();
    fireEvent.click(screen.getByLabelText("vip"));
    fireEvent.click(screen.getByLabelText("default"));
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    await waitFor(() =>
      expect(lastQuery().getAll("f")).toEqual(["cos:in:vip,default"]),
    );
    expect(
      screen.getByText("Class of service is vip, default"),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Filters ▾" }));
    fireEvent.change(screen.getByLabelText("Field"), {
      target: { value: "is_admin" },
    });
    fireEvent.change(screen.getByLabelText("Value"), {
      target: { value: "false" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    await waitFor(() =>
      expect(lastQuery().getAll("f")).toContain("is_admin:is:false"),
    );
    expect(screen.getByText("Administrator is no")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Filters ▾" }));
    fireEvent.change(screen.getByLabelText("Field"), {
      target: { value: "last_login_at" },
    });
    fireEvent.change(screen.getByLabelText("Condition"), {
      target: { value: "before" },
    });
    fireEvent.change(screen.getByLabelText("Value"), {
      target: { value: "2026-01-01" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    await waitFor(() =>
      expect(lastQuery().getAll("f")).toContain(
        "last_login_at:before:2026-01-01",
      ),
    );

    fireEvent.click(screen.getByRole("button", { name: "Filters ▾" }));
    fireEvent.change(screen.getByLabelText("Field"), {
      target: { value: "domain" },
    });
    fireEvent.change(screen.getByLabelText("Condition"), {
      target: { value: "eq" },
    });
    fireEvent.change(screen.getByLabelText("Value"), {
      target: { value: "other.org" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    await waitFor(() =>
      expect(lastQuery().getAll("f")).toContain("domain:eq:other.org"),
    );

    fireEvent.click(screen.getByRole("button", { name: "Filters ▾" }));
    fireEvent.change(screen.getByLabelText("Field"), {
      target: { value: "email" },
    });
    fireEvent.change(screen.getByLabelText("Condition"), {
      target: { value: "not_set" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    await waitFor(() =>
      expect(lastQuery().getAll("f")).toContain("email:not_set"),
    );

    fireEvent.click(screen.getByRole("button", { name: "Filters ▾" }));
    fireEvent.click(screen.getByRole("button", { name: "Clear all filters" }));
    await waitFor(() => expect(lastQuery().getAll("f")).toEqual([]));
    // Clicking outside closes the menu.
    fireEvent.mouseDown(document.body);
    expect(screen.queryByRole("dialog", { name: "Filters" })).toBeNull();
  });

  it("sorts by clicking headers and pages", async () => {
    const { lastQuery } = open();
    await screen.findByText("Ana Lopez");
    fireEvent.click(screen.getByRole("button", { name: "Usage" }));
    await waitFor(() => expect(lastQuery().get("sort")).toBe("usage_pct"));
    expect(lastQuery().get("order")).toBe("asc");
    fireEvent.click(await screen.findByRole("button", { name: "Usage ▲" }));
    await waitFor(() => expect(lastQuery().get("order")).toBe("desc"));
    fireEvent.change(screen.getByLabelText("Per page"), {
      target: { value: "100" },
    });
    await waitFor(() => expect(lastQuery().get("limit")).toBe("100"));
  });

  it("selects accounts and queues a bulk backup", async () => {
    const { calls } = open();
    await screen.findByText("Ana Lopez");
    fireEvent.click(screen.getByLabelText("Select page"));
    expect(screen.getByText("2 accounts selected")).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("Select ben@example.org"));
    expect(screen.getByText("1 account selected")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Full backup" }));
    expect(await screen.findByRole("status")).toHaveTextContent(
      "1 full backup queued (1 already running, 0 deleted).",
    );
    const post = calls.find((c) => c.method === "POST");
    expect(post?.body).toEqual({ account_ids: ["a1"], backup_type: "full" });
    expect(screen.queryByText(/selected/)).toBeNull();

    fireEvent.click(screen.getByLabelText("Select page"));
    fireEvent.click(screen.getByLabelText("Select page")); // toggles off
    expect(screen.queryByText(/selected/)).toBeNull();
    fireEvent.click(screen.getByLabelText("Select ana@example.org"));
    fireEvent.click(screen.getByRole("button", { name: "Clear selection" }));
    expect(screen.queryByText(/selected/)).toBeNull();
  });

  it("reports bulk errors and hides actions outside the user's domains", async () => {
    open(user("domain_admin", DOMAIN_ID), [item(), cy], {
      "POST /api/backups/bulk": { status: 403, body: { detail: "forbidden" } },
    });
    await screen.findByText("Ana Lopez");
    fireEvent.click(screen.getByLabelText("Select ana@example.org"));
    fireEvent.click(screen.getByRole("button", { name: "Incremental backup" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("forbidden");
    fireEvent.click(screen.getByLabelText("Select cy@other.org"));
    expect(
      screen.queryByRole("button", { name: "Incremental backup" }),
    ).toBeNull();
    expect(
      screen.queryByRole("button", { name: "Create a policy" }),
    ).toBeNull();
  });

  it("creates a policy from the selection", async () => {
    let created: unknown;
    open(user("super_admin"), [item(), ben], {
      "POST /api/policies": (c: { body: unknown }) => {
        created = c.body;
        return { status: 201, body: {} };
      },
    });
    await screen.findByText("Ana Lopez");
    fireEvent.click(screen.getByLabelText("Select page"));
    fireEvent.click(screen.getByRole("button", { name: "Create a policy" }));
    expect(
      await screen.findByRole("heading", { name: "New policy" }),
    ).toBeInTheDocument();
    expect(screen.getByText("2 selected accounts")).toBeInTheDocument();
    expect(
      screen.getByText(/ana@example.org, ben@example.org/),
    ).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Name"), {
      target: { value: "VIP" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(created).toBeDefined());
    expect(created).toMatchObject({
      name: "VIP",
      target_type: "accounts",
      target_id: null,
      account_ids: ["a1", "a2"],
    });
  });

  it("shows an empty result", async () => {
    open(user("viewer"), []);
    expect(await screen.findByText("No account.")).toBeInTheDocument();
  });
});

describe("grouping and favourites", () => {
  it("groups accounts by a field and opens a group", async () => {
    const { calls } = open(user("super_admin"), [item()], {
      "GET /api/accounts/groups": [
        { value: "active", count: 2, used_bytes: 2048, stored_bytes: 0 },
        { value: "locked", count: 120, used_bytes: 0, stored_bytes: 0 },
      ],
    });
    await screen.findByText("ana@example.org");
    fireEvent.click(screen.getByRole("button", { name: /Group by/ }));
    fireEvent.click(screen.getByRole("radio", { name: "Status" }));
    const locked = await screen.findByRole("button", { name: /locked/ });
    expect(screen.getByText("2 group(s), 122 account(s)")).toBeInTheDocument();
    const groupCall = calls.find((c) =>
      c.url.startsWith("/api/accounts/groups"),
    );
    expect(groupCall?.url).toContain("group_by=status");
    fireEvent.click(locked);
    expect(await screen.findByText("ana@example.org")).toBeInTheDocument();
    const opened = calls
      .filter((c) => c.url.startsWith("/api/accounts?"))
      .pop();
    expect(decodeURIComponent(opened?.url ?? "")).toContain(
      "f=status:in:locked",
    );
    // More accounts than shown: drill into the group (filter added, grouping removed).
    fireEvent.click(
      screen.getByRole("button", { name: /See the 120 accounts/ }),
    );
    expect(await screen.findByText("Status is locked")).toBeInTheDocument();
    expect(window.location.search).not.toContain("group=");
  });

  it("applies, saves, shares and deletes favourites", async () => {
    let saved: unknown;
    const { lastQuery } = open(user("super_admin"), [item()], {
      "GET /api/views": [
        {
          id: "v1",
          page: "accounts",
          name: "No policy",
          query: "f=has_policy%3Ais%3Afalse",
          shared: false,
          owner: "alice",
          mine: true,
          created_at: "2026-11-20T00:00:00Z",
        },
        {
          id: "v2",
          page: "accounts",
          name: "By domain",
          query: "group=domain",
          shared: true,
          owner: "bob",
          mine: false,
          created_at: "2026-11-20T00:00:00Z",
        },
      ],
      "POST /api/views": (c: { body: unknown }) => {
        saved = c.body;
        return { status: 201, body: { id: "v3", ...(c.body as object) } };
      },
      "DELETE /api/views/v1": { status: 204, body: undefined },
    });
    await screen.findByText("ana@example.org");
    fireEvent.click(screen.getByRole("button", { name: /Favourites/ }));
    expect(await screen.findByText("shared by bob")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Delete view By domain" }),
    ).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "No policy" }));
    await waitFor(() =>
      expect(lastQuery().getAll("f")).toEqual(["has_policy:is:false"]),
    );

    fireEvent.click(screen.getByRole("button", { name: /Favourites/ }));
    fireEvent.change(await screen.findByLabelText("Name"), {
      target: { value: "Sans politique" },
    });
    fireEvent.click(screen.getByLabelText("Share with the other users"));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(saved).toBeDefined());
    expect(saved).toEqual({
      page: "accounts",
      name: "Sans politique",
      query: "f=has_policy%3Ais%3Afalse",
      shared: true,
    });
    vi.spyOn(window, "confirm").mockReturnValue(true);
    fireEvent.click(
      screen.getByRole("button", { name: "Delete view No policy" }),
    );
    await waitFor(() =>
      expect(
        (
          globalThis.fetch as unknown as {
            mock: { calls: [string, RequestInit][] };
          }
        ).mock.calls.some(
          ([url, init]) => url === "/api/views/v1" && init.method === "DELETE",
        ),
      ).toBe(true),
    );
  });

  it("builds the filters and labels of a group", () => {
    const t = i18n.t.bind(i18n);
    expect(groupFilters("status", "choice", "active")).toEqual([
      "status:in:active",
    ]);
    expect(groupFilters("is_admin", "boolean", "true")).toEqual([
      "is_admin:is:true",
    ]);
    expect(groupFilters("domain", "text", "a.dz")).toEqual(["domain:eq:a.dz"]);
    expect(groupFilters("domain", "text", null)).toEqual(["domain:not_set"]);
    expect(groupFilters("last_login_at", "date", "2026-03")).toEqual([
      "last_login_at:after:2026-02-28",
      "last_login_at:before:2026-04-01",
    ]);
    expect(groupLabel("status", "choice", "locked", t)).toBe("locked");
    expect(groupLabel("is_admin", "boolean", "false", t)).toBe(t("no"));
    expect(groupLabel("domain", "text", null, t)).toBe("Not set");
    expect(groupLabel("last_login_at", "date", "2026-03", t)).toMatch(/2026/);
  });
});

describe("filter labels", () => {
  it("describes presets and raw filters", () => {
    const t = i18n.getFixedT("en");
    expect(filterLabel("has_policy:is:false", t)).toBe("Without policy");
    expect(filterLabel("status:in:locked,closed", t)).toBe("Locked or closed");
    expect(filterLabel("last_backup_status:not_in:partial", t)).toBe(
      "Last backup status is not partial",
    );
    expect(filterLabel("policy:contains:nuit", t)).toBe(
      "Policies contains nuit",
    );
    expect(filterLabel("display_name:set", t)).toBe("Display name is set");
    expect(filterLabel("unknown:eq:1", t)).toBe("unknown = 1");
    expect(filterLabel("is_admin:is:false", t)).toBe("Administrator is no");
    expect(filterLabel("usage_pct:gt:50", t)).toBe("Usage > 50");
  });
});
