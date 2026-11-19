import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "../App";
import type { User } from "../api/types";
import i18n from "../i18n";
import {
  DOMAIN_ID,
  dashboard,
  fakeApi,
  job,
  page,
  user,
} from "../test/fakeApi";

beforeEach(async () => {
  await i18n.changeLanguage("en");
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const basics = {
  "GET /api/dashboard": dashboard,
  "GET /api/jobs": page([job()]),
  "GET /api/policies": [],
  "GET /api/domains": page([{ id: DOMAIN_ID, name: "lnhc.dz" }]),
};

function at(path: string) {
  window.history.pushState({}, "", path);
  render(<App />);
}

describe("two-step sign-in", () => {
  it("asks for the code, refuses a bad one, then signs in", async () => {
    let attempts = 0;
    const calls = fakeApi({
      ...basics,
      "GET /api/auth/me": { status: 401, body: { detail: "no session" } },
      "POST /api/auth/login": (c) => {
        attempts += 1;
        const body = c.body as { otp?: string };
        if (!body.otp) return { status: 401, body: { detail: "otp_required" } };
        if (body.otp !== "123456")
          return { status: 401, body: { detail: "invalid credentials" } };
        return { body: { user: user("super_admin"), csrf_token: "c1" } };
      },
    });
    at("/");
    fireEvent.change(await screen.findByLabelText("Username"), {
      target: { value: "ana" },
    });
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "pw" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    const code = await screen.findByLabelText("Code");
    expect(screen.getByText(/6-digit code/)).toBeInTheDocument();
    fireEvent.change(code, { target: { value: "000000" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(
      await screen.findByText("Invalid or already used code."),
    ).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Code"), {
      target: { value: "123456" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(
      await screen.findByRole("heading", { name: "Dashboard" }),
    ).toBeInTheDocument();
    expect(attempts).toBe(3);
    expect(
      calls.filter((c) => c.url === "/api/auth/login").at(-1)?.body,
    ).toEqual({
      username: "ana",
      password: "pw",
      otp: "123456",
    });
  });

  it("goes back to the password step", async () => {
    fakeApi({
      ...basics,
      "GET /api/auth/me": { status: 401, body: { detail: "no session" } },
      "POST /api/auth/login": { status: 401, body: { detail: "otp_required" } },
    });
    at("/");
    fireEvent.change(await screen.findByLabelText("Username"), {
      target: { value: "ana" },
    });
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "pw" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    fireEvent.click(
      await screen.findByRole("button", { name: /Back to the username/ }),
    );
    expect(screen.getByLabelText("Password")).toHaveValue("");
  });
});

describe("enrolment", () => {
  it("is forced by the policy, then shows the recovery codes", async () => {
    let me: User = { ...user("super_admin"), totp_enrollment_required: true };
    const calls = fakeApi({
      ...basics,
      "GET /api/auth/me": () => ({ body: me }),
      "POST /api/auth/2fa/setup": {
        secret: "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP",
        uri: "otpauth://totp/x",
        qr_svg: "<svg xmlns='http://www.w3.org/2000/svg'/>",
      },
      "POST /api/auth/2fa/enable": (c) => {
        if ((c.body as { code: string }).code !== "654321")
          return { status: 422, body: { detail: "invalid code" } };
        me = { ...me, totp_enabled: true, totp_enrollment_required: false };
        return { body: { recovery_codes: ["abcd-efgh", "jkmn-pqrs"] } };
      },
    });
    at("/");
    expect(
      await screen.findByRole("heading", {
        name: "Two-factor authentication required",
      }),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Start" }));
    const qr = await screen.findByAltText(/QR code/);
    expect(qr.getAttribute("src")).toMatch(/^data:image\/svg\+xml/);
    expect(
      screen.getByText("JBSW Y3DP EHPK 3PXP JBSW Y3DP EHPK 3PXP"),
    ).toBeInTheDocument();
    const input = screen.getByLabelText(/6-digit code shown by the app/);
    fireEvent.change(input, { target: { value: "111111" } });
    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));
    expect(await screen.findByText(/Code refused/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText(/6-digit code shown by the app/), {
      target: { value: "654321" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));
    expect(await screen.findByText("abcd-efgh")).toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", { name: "I have saved my codes" }),
    );
    expect(
      await screen.findByRole("heading", { name: "Dashboard" }),
    ).toBeInTheDocument();
    expect(calls.some((c) => c.url === "/api/auth/2fa/enable")).toBe(true);
  });
});

describe("my account", () => {
  it("changes the password and manages 2FA", async () => {
    let me: User = {
      ...user("viewer"),
      totp_enabled: true,
      recovery_codes_left: 7,
    };
    const calls = fakeApi({
      ...basics,
      "GET /api/auth/me": () => ({ body: me }),
      "POST /api/auth/password": (c) =>
        (c.body as { current_password: string }).current_password === "old"
          ? { status: 204, body: undefined }
          : { status: 403, body: { detail: "current password is wrong" } },
      "POST /api/auth/2fa/recovery-codes": { recovery_codes: ["xxxx-yyyy"] },
      "POST /api/auth/2fa/disable": () => {
        me = { ...me, totp_enabled: false };
        return { status: 204, body: undefined };
      },
    });
    at("/me");
    expect(
      await screen.findByRole("heading", { name: "My account" }),
    ).toBeInTheDocument();
    expect(screen.getByText("7 recovery code(s) left.")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Current password"), {
      target: { value: "bad" },
    });
    fireEvent.change(screen.getByLabelText("New password"), {
      target: { value: "n".repeat(12) },
    });
    fireEvent.change(screen.getByLabelText("Confirm"), {
      target: { value: "x".repeat(12) },
    });
    fireEvent.click(screen.getByRole("button", { name: "Change password" }));
    expect(
      await screen.findByText("The two passwords do not match."),
    ).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Confirm"), {
      target: { value: "n".repeat(12) },
    });
    fireEvent.click(screen.getByRole("button", { name: "Change password" }));
    expect(
      await screen.findByText("Error: current password is wrong"),
    ).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Current password"), {
      target: { value: "old" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Change password" }));
    expect(await screen.findByText(/Password changed/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText(/Current code/), {
      target: { value: "123456" },
    });
    fireEvent.click(
      screen.getByRole("button", { name: "Generate new recovery codes" }),
    );
    expect(await screen.findByText("xxxx-yyyy")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText(/Current code/), {
      target: { value: "123456" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Turn off" }));
    expect(
      await screen.findByRole("button", { name: /Turn on two-factor/ }),
    ).toBeInTheDocument();
    expect(calls.find((c) => c.url === "/api/auth/2fa/disable")?.body).toEqual({
      code: "123456",
    });
  });

  it("explains that directory passwords are changed elsewhere", async () => {
    fakeApi({
      ...basics,
      "GET /api/auth/me": { ...user("viewer"), auth_source: "ldap" },
    });
    at("/me");
    expect(
      await screen.findByText(/managed by the directory/),
    ).toBeInTheDocument();
  });
});

describe("access accounts", () => {
  const ana: User = {
    id: "u2",
    username: "ana",
    display_name: "Ana",
    email: null,
    auth_source: "local",
    roles: [{ id: "r9", role: "domain_admin", domain_id: DOMAIN_ID }],
    is_active: true,
    locked_until: new Date(Date.now() + 3600_000).toISOString(),
    last_login_at: null,
    totp_enabled: true,
  };

  it("lists, creates, edits roles, resets and deletes", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const calls = fakeApi({
      ...basics,
      "GET /api/auth/me": user("super_admin"),
      "GET /api/users": page([user("super_admin"), ana]),
      "POST /api/users": (c) => ({
        status: 201,
        body: { ...ana, id: "u3", ...(c.body as object), roles: [] },
      }),
      "PATCH /api/users/u2": (c) => ({
        body: { ...ana, ...(c.body as object) },
      }),
      "POST /api/users/u2/roles": { status: 201, body: ana },
      "DELETE /api/users/u2/roles/r9": { body: { ...ana, roles: [] } },
      "DELETE /api/users/u2": { status: 204, body: undefined },
      "GET /api/settings/security": { require_2fa: "none" },
      "PUT /api/settings/security": (c) => ({ body: c.body }),
    });
    at("/users");
    const row = (await screen.findByText("ana")).closest("tr") as HTMLElement;
    expect(
      within(row).getByText("Domain administrator (lnhc.dz)"),
    ).toBeInTheDocument();
    expect(within(row).getByText("locked")).toBeInTheDocument();
    expect(within(row).getByText("on")).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Access accounts" }),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "New account" }));
    fireEvent.change(screen.getByLabelText("Username"), {
      target: { value: "bob" },
    });
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "p".repeat(12) },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() =>
      expect(
        calls.find((c) => c.url === "/api/users" && c.method === "POST")?.body,
      ).toEqual({
        username: "bob",
        auth_source: "local",
        password: "p".repeat(12),
        display_name: null,
        email: null,
      }),
    );

    fireEvent.click(within(row).getByRole("button", { name: "Edit" }));
    const editor = await screen.findByRole("region", { name: "Account ana" });
    fireEvent.change(within(editor).getByLabelText("Add role"), {
      target: { value: "restore_operator" },
    });
    fireEvent.change(within(editor).getByLabelText("Domain"), {
      target: { value: DOMAIN_ID },
    });
    fireEvent.click(within(editor).getByRole("button", { name: "Add" }));
    expect(await within(editor).findByText("Role added.")).toBeInTheDocument();
    expect(calls.find((c) => c.url === "/api/users/u2/roles")?.body).toEqual({
      role: "restore_operator",
      domain_id: DOMAIN_ID,
    });
    fireEvent.click(within(editor).getByRole("button", { name: "remove" }));
    expect(await within(editor).findByText(/Role removed/)).toBeInTheDocument();
    fireEvent.change(within(editor).getByLabelText("New password"), {
      target: { value: "q".repeat(12) },
    });
    fireEvent.click(
      within(editor).getByRole("button", { name: "Reset password" }),
    );
    expect(
      await within(editor).findByText(/Password reset/),
    ).toBeInTheDocument();
    fireEvent.click(
      within(editor).getByRole("button", {
        name: "Reset two-factor authentication",
      }),
    );
    expect(
      await within(editor).findByText("Two-factor authentication reset."),
    ).toBeInTheDocument();
    fireEvent.click(within(editor).getByRole("button", { name: "Unlock" }));
    expect(
      await within(editor).findByText("Account unlocked."),
    ).toBeInTheDocument();
    fireEvent.click(within(editor).getByRole("button", { name: "Disable" }));
    expect(
      await within(editor).findByText(/Account disabled/),
    ).toBeInTheDocument();
    const patches = calls
      .filter((c) => c.method === "PATCH")
      .map((c) => c.body);
    expect(patches).toEqual([
      { password: "q".repeat(12) },
      { reset_2fa: true },
      { unlock: true },
      { is_active: false },
    ]);
    fireEvent.click(within(editor).getByRole("button", { name: "Delete" }));
    await waitFor(() =>
      expect(
        calls.some((c) => c.method === "DELETE" && c.url === "/api/users/u2"),
      ).toBe(true),
    );

    fireEvent.click(screen.getByRole("link", { name: "Settings → Security" }));
    fireEvent.click(await screen.findByLabelText(/every account/));
    expect(await screen.findByText("Policy saved.")).toBeInTheDocument();
    expect(calls.find((c) => c.method === "PUT")?.body).toEqual({
      require_2fa: "all",
    });
  });

  it("is reserved to super administrators", async () => {
    fakeApi({ ...basics, "GET /api/auth/me": user("domain_admin", DOMAIN_ID) });
    at("/users");
    expect(
      await screen.findByText("Super-administrators only."),
    ).toBeInTheDocument();
  });
});
