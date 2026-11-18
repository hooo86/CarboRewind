import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import { setCsrfToken } from "./api/client";
import i18n, { setLanguage } from "./i18n";
import {
  ACCOUNT_ID,
  DOMAIN_ID,
  account,
  dashboard,
  fakeApi,
  job,
  neverBacked,
  page,
  user,
} from "./test/fakeApi";

function renderAt(path: string) {
  window.history.pushState({}, "", path);
  return render(<App />);
}

const domains = page([
  { id: DOMAIN_ID, name: "example.org", account_count: 2 },
]);

beforeEach(async () => {
  await i18n.changeLanguage("en");
});

afterEach(() => {
  vi.unstubAllGlobals();
  setCsrfToken(undefined);
  vi.restoreAllMocks();
});

describe("authentication", () => {
  it("shows the login form when there is no session, then signs in", async () => {
    const calls = fakeApi({
      "GET /api/auth/me": {
        status: 401,
        body: { detail: "not authenticated" },
      },
      "POST /api/auth/login": (c) =>
        (c.body as { password: string }).password === "good"
          ? { body: { user: user("super_admin"), csrf_token: "csrf-1" } }
          : { status: 401, body: { detail: "invalid credentials" } },
      "GET /api/dashboard": dashboard,
      "GET /api/jobs": page([job()]),
    });
    renderAt("/");
    const username = await screen.findByLabelText("Username");
    fireEvent.change(username, { target: { value: "alice" } });
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "bad" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Invalid credentials.",
    );

    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "good" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(
      await screen.findByRole("heading", { name: "Dashboard" }),
    ).toBeInTheDocument();
    expect(await screen.findByText("42")).toBeInTheDocument();
    expect(screen.getByText("5 GB")).toBeInTheDocument();
    expect(await screen.findByText("bob@example.org")).toBeInTheDocument();
    const login = calls.filter((c) => c.url === "/api/auth/login").at(-1);
    expect(login?.body).toEqual({ username: "alice", password: "good" });
  });

  it("reports throttling and other login failures", async () => {
    let status = 429;
    fakeApi({
      "GET /api/auth/me": { status: 401, body: {} },
      "POST /api/auth/login": () => ({ status, body: { detail: "boom" } }),
    });
    renderAt("/");
    fireEvent.change(await screen.findByLabelText("Username"), {
      target: { value: "a" },
    });
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "b" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Too many attempts",
    );
    status = 500;
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "b" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(
      await screen.findByText("Sign-in failed (boom)."),
    ).toBeInTheDocument();
  });

  it("signs out with the CSRF token from the cookie", async () => {
    // jsdom refuses __Host- cookies over http, so fake the cookie jar.
    vi.spyOn(document, "cookie", "get").mockReturnValue(
      "a=b; __Host-carborewind_csrf=cookie-token",
    );
    const calls = fakeApi({
      "GET /api/auth/me": user("viewer"),
      "GET /api/dashboard": {
        ...dashboard,
        agents_active: null,
        last_agent_heartbeat: null,
      },
      "GET /api/jobs": page([]),
      "POST /api/auth/logout": { status: 204, body: undefined },
    });
    renderAt("/");
    fireEvent.click(await screen.findByRole("button", { name: "Sign out" }));
    expect(await screen.findByLabelText("Username")).toBeInTheDocument();
    const logout = calls.find((c) => c.url === "/api/auth/logout");
    expect(logout?.headers["X-CSRF-Token"]).toBe("cookie-token");
  });

  it("returns to the login form when the session expires", async () => {
    let expired = false;
    fakeApi({
      "GET /api/auth/me": user("viewer"),
      "GET /api/dashboard": dashboard,
      "GET /api/jobs": () =>
        expired ? { status: 401, body: {} } : { body: page([]) },
      "GET /api/domains": () => ({ status: 401, body: {} }),
    });
    renderAt("/");
    await screen.findByRole("heading", { name: "Dashboard" });
    expired = true;
    fireEvent.click(screen.getByRole("link", { name: "Accounts" }));
    expect(await screen.findByLabelText("Username")).toBeInTheDocument();
  });
});

describe("accounts", () => {
  it("lists accounts and queues a backup with the CSRF header", async () => {
    const calls = fakeApi({
      "GET /api/auth/me": user("domain_admin", DOMAIN_ID),
      "GET /api/domains": domains,
      [`GET /api/domains/${DOMAIN_ID}/accounts`]: page([account, neverBacked]),
      "POST /api/backups": (c) => ({
        status: 202,
        body: job({
          status: "queued",
          backup_type: (c.body as { backup_type: "full" }).backup_type,
        }),
      }),
    });
    setCsrfToken("csrf-xyz");
    renderAt("/accounts");
    expect(await screen.findByText("bob@example.org")).toBeInTheDocument();
    expect(screen.getByText("never")).toBeInTheDocument();
    expect(screen.getByText("1–2 of 2")).toBeInTheDocument();

    fireEvent.click(
      screen.getByRole("button", {
        name: "Start a full backup of bob@example.org",
      }),
    );
    expect(await screen.findByRole("status")).toHaveTextContent(
      "full backup of bob@example.org queued.",
    );
    const post = calls.find((c) => c.method === "POST");
    expect(post?.headers["X-CSRF-Token"]).toBe("csrf-xyz");
    expect(post?.body).toEqual({ account_id: ACCOUNT_ID, backup_type: "full" });
  });

  it("shows API refusals and searches", async () => {
    const calls = fakeApi({
      "GET /api/auth/me": user("super_admin"),
      "GET /api/domains": domains,
      [`GET /api/domains/${DOMAIN_ID}/accounts`]: page([account]),
      "POST /api/backups": { status: 403, body: { detail: "forbidden" } },
    });
    renderAt("/accounts");
    fireEvent.click(
      await screen.findByRole("button", {
        name: "Start a incremental backup of bob@example.org",
      }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Error: forbidden",
    );

    fireEvent.change(screen.getByRole("searchbox"), {
      target: { value: " bob " },
    });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() =>
      expect(calls.some((c) => c.url.includes("/accounts?q=bob"))).toBe(true),
    );
  });

  it("hides backup buttons from viewers and handles empty inventories", async () => {
    fakeApi({
      "GET /api/auth/me": user("viewer"),
      "GET /api/domains": domains,
      [`GET /api/domains/${DOMAIN_ID}/accounts`]: page([account]),
    });
    renderAt("/accounts");
    expect(await screen.findByText("bob@example.org")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /backup of/ }),
    ).not.toBeInTheDocument();
  });

  it("explains an empty inventory", async () => {
    fakeApi({
      "GET /api/auth/me": user("viewer"),
      "GET /api/domains": page([]),
    });
    renderAt("/accounts");
    expect(
      await screen.findByText(/No domain inventoried yet/),
    ).toBeInTheDocument();
  });

  it("pages through accounts", async () => {
    const calls = fakeApi({
      "GET /api/auth/me": user("viewer"),
      "GET /api/domains": domains,
      [`GET /api/domains/${DOMAIN_ID}/accounts`]: (c) => ({
        body: {
          items: [c.url.includes("offset=50") ? neverBacked : account],
          total: 51,
          limit: 50,
          offset: c.url.includes("offset=50") ? 50 : 0,
        },
      }),
    });
    renderAt("/accounts");
    await screen.findByText("bob@example.org");
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(await screen.findByText("carol@example.org")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Previous" }));
    expect(await screen.findByText("bob@example.org")).toBeInTheDocument();
    expect(
      calls.filter((c) => c.url.includes("offset=0")).length,
    ).toBeGreaterThan(1);
  });
});

describe("account detail", () => {
  it("shows snapshots and jobs, and starts a backup", async () => {
    const calls = fakeApi({
      "GET /api/auth/me": user("super_admin"),
      [`GET /api/accounts/${ACCOUNT_ID}`]: {
        id: ACCOUNT_ID,
        email: "bob@example.org",
        status: "active",
        domain_id: DOMAIN_ID,
        domain: "example.org",
        last_seen_at: null,
        snapshot_count: 1,
        last_snapshot: null,
      },
      [`GET /api/accounts/${ACCOUNT_ID}/snapshots`]: page([
        {
          id: "s1",
          seq: 7,
          backup_type: "full",
          status: "partial",
          taken_at: "2026-11-18T09:01:10Z",
          item_count: 1234,
          size_bytes: 2048,
          new_bytes: 512,
        },
      ]),
      "GET /api/jobs": page([
        job({ status: "failed", error: "SOAP timeout", finished_at: null }),
      ]),
      "POST /api/backups": { status: 202, body: job({ status: "queued" }) },
    });
    renderAt(`/accounts/${ACCOUNT_ID}`);
    expect(
      await screen.findByRole("heading", { name: "bob@example.org" }),
    ).toBeInTheDocument();
    expect(await screen.findByText("partial")).toBeInTheDocument();
    expect(screen.getByText("2 KB")).toBeInTheDocument();
    expect(await screen.findByText("SOAP timeout")).toBeInTheDocument();
    expect(calls.some((c) => c.url.includes(`account_id=${ACCOUNT_ID}`))).toBe(
      true,
    );

    fireEvent.click(
      screen.getByRole("button", {
        name: "Start a incremental backup of bob@example.org",
      }),
    );
    expect(await screen.findByRole("status")).toHaveTextContent("queued");
  });

  it("shows an error with retry for unknown accounts", async () => {
    fakeApi({ "GET /api/auth/me": user("viewer"), "GET /api/jobs": page([]) });
    renderAt(`/accounts/${ACCOUNT_ID}`);
    const alerts = await screen.findAllByRole("alert");
    expect(alerts[0]).toHaveTextContent("Error: not found");
    fireEvent.click(
      within(alerts[0] as HTMLElement).getByRole("button", { name: "Retry" }),
    );
    expect(await screen.findByText("No job.")).toBeInTheDocument();
    expect(screen.getAllByRole("alert").length).toBeGreaterThan(0);
  });
});

describe("jobs page", () => {
  it("filters by status and translates to French", async () => {
    const calls = fakeApi({
      "GET /api/auth/me": user("viewer"),
      "GET /api/jobs": page([
        job({
          status: "running",
          finished_at: null,
          started_at: new Date().toISOString(),
        }),
        job({
          id: "j2",
          kind: "restore",
          backup_type: null,
          account_id: null,
          account_email: null,
        }),
      ]),
    });
    renderAt("/jobs?status=bogus");
    expect((await screen.findAllByText("running")).length).toBeGreaterThan(0);
    expect(screen.getByText("1 min 5 s")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Filter"), {
      target: { value: "failed" },
    });
    await waitFor(() =>
      expect(calls.some((c) => c.url.includes("status=failed"))).toBe(true),
    );

    await act(async () => setLanguage("fr"));
    expect(
      await screen.findByRole("heading", { name: "Jobs" }),
    ).toBeInTheDocument();
    expect(screen.getAllByText("en cours").length).toBeGreaterThan(0);
    expect(
      screen.getByRole("link", { name: "Tableau de bord" }),
    ).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Langue"), {
      target: { value: "en" },
    });
    expect(
      await screen.findByRole("link", { name: "Dashboard" }),
    ).toBeInTheDocument();
  });

  it("redirects unknown routes to the dashboard", async () => {
    fakeApi({
      "GET /api/auth/me": user("viewer"),
      "GET /api/dashboard": dashboard,
      "GET /api/jobs": page([]),
    });
    renderAt("/nope");
    expect(
      await screen.findByRole("heading", { name: "Dashboard" }),
    ).toBeInTheDocument();
  });
});
