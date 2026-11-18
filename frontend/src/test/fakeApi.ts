import { vi } from "vitest";

import type { AccountRow, Dashboard, Job, User } from "../api/types";

export interface Call {
  method: string;
  url: string;
  headers: Record<string, string>;
  body: unknown;
}

type Handler = (call: Call) => { status?: number; body?: unknown } | undefined;

function isSpec(route: unknown): route is { status: number; body: unknown } {
  return (
    typeof route === "object" &&
    route !== null &&
    "body" in route &&
    typeof (route as { status?: unknown }).status === "number"
  );
}

/** Install a fetch stub that routes "METHOD /path" (query ignored) to handlers. */
export function fakeApi(routes: Record<string, Handler | object>) {
  const calls: Call[] = [];
  const fn = vi.fn((input: string, init: RequestInit = {}) => {
    const method = init.method ?? "GET";
    const call: Call = {
      method,
      url: input,
      headers: (init.headers ?? {}) as Record<string, string>,
      body: typeof init.body === "string" ? JSON.parse(init.body) : undefined,
    };
    calls.push(call);
    const path = input.split("?")[0];
    const route = routes[`${method} ${path}`];
    let status = 404;
    let body: unknown = { detail: "not found" };
    if (typeof route === "function") {
      const out = (route as Handler)(call) ?? {};
      status = out.status ?? 200;
      body = out.body;
    } else if (isSpec(route)) {
      status = route.status;
      body = route.body;
    } else if (route !== undefined) {
      status = 200;
      body = route;
    }
    return Promise.resolve({
      ok: status >= 200 && status < 300,
      status,
      statusText: `status ${status}`,
      json: () =>
        body === undefined
          ? Promise.reject(new Error("no body"))
          : Promise.resolve(body),
    });
  });
  vi.stubGlobal("fetch", fn);
  return calls;
}

export const page = <T>(items: T[]) => ({
  items,
  total: items.length,
  limit: 50,
  offset: 0,
});

export const DOMAIN_ID = "d0000000-0000-0000-0000-000000000001";
export const ACCOUNT_ID = "a0000000-0000-0000-0000-000000000001";

export function user(
  role: User["roles"][number]["role"],
  domainId: string | null = null,
): User {
  return {
    id: "u1",
    username: "alice",
    display_name: "Alice",
    email: null,
    auth_source: "local",
    roles: [{ id: "r1", role, domain_id: domainId }],
  };
}

export const account: AccountRow = {
  id: ACCOUNT_ID,
  email: "bob@example.org",
  status: "active",
  last_seen_at: "2026-11-18T08:00:00Z",
  last_backup_at: "2026-11-18T09:00:00Z",
  last_backup_status: "complete",
  last_backup_type: "full",
};

export const neverBacked: AccountRow = {
  ...account,
  id: "a0000000-0000-0000-0000-000000000002",
  email: "carol@example.org",
  last_backup_at: null,
  last_backup_status: null,
  last_backup_type: null,
};

export const job = (over: Partial<Job> = {}): Job => ({
  id: "j1",
  kind: "backup",
  status: "succeeded",
  account_id: ACCOUNT_ID,
  account_email: "bob@example.org",
  backup_type: "incremental",
  attempts: 1,
  progress: null,
  error: null,
  created_at: "2026-11-18T09:00:00Z",
  started_at: "2026-11-18T09:00:05Z",
  finished_at: "2026-11-18T09:01:10Z",
  ...over,
});

export const dashboard: Dashboard = {
  accounts: 42,
  protected_24h: 40,
  never_backed_up: 1,
  partial_last: 1,
  jobs_queued: 2,
  jobs_running: 1,
  jobs_failed_24h: 3,
  stored_bytes: 5 * 1024 ** 3,
  agents_active: 1,
  last_agent_heartbeat: new Date().toISOString(),
};
