import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { App } from "../App";
import type { AccountDetail } from "../api/types";
import i18n from "../i18n";
import { ACCOUNT_ID, DOMAIN_ID, fakeApi, page, user } from "../test/fakeApi";

const snapshot = {
  id: "s1",
  seq: 3,
  backup_type: "full",
  status: "complete",
  taken_at: "2026-11-20T01:00:00Z",
  item_count: 10,
  size_bytes: 1000,
  new_bytes: 10,
} as const;

function open(over: Partial<AccountDetail> = {}, role = user("super_admin")) {
  const posted: Record<string, unknown> = {};
  const calls = fakeApi({
    "GET /api/auth/me": role,
    [`GET /api/accounts/${ACCOUNT_ID}`]: {
      id: ACCOUNT_ID,
      email: "h.badi@lnhc.dz",
      status: "active",
      domain_id: DOMAIN_ID,
      domain: "lnhc.dz",
      last_seen_at: null,
      snapshot_count: 3,
      last_snapshot: snapshot,
      ...over,
    },
    [`GET /api/accounts/${ACCOUNT_ID}/snapshots`]: page([snapshot]),
    "GET /api/jobs": page([]),
    [`POST /api/mail/accounts/${ACCOUNT_ID}/unlock`]: (c: {
      body: unknown;
    }) => {
      posted.unlock = c.body;
      return { body: { id: "a1", action: "unlock", queued: true } };
    },
    [`POST /api/mail/accounts/${ACCOUNT_ID}/delete`]: (c: {
      body: unknown;
    }) => {
      posted.delete = c.body;
      return { body: { id: "a2", action: "delete", queued: true } };
    },
    [`POST /api/mail/accounts/${ACCOUNT_ID}/password`]: (c: {
      body: unknown;
    }) => {
      posted.password = c.body;
      return { body: { id: "a3", action: "password", queued: true } };
    },
    [`POST /api/archives/accounts/${ACCOUNT_ID}/legal-hold`]: (c: {
      body: unknown;
    }) => {
      posted.archive = c.body;
      return {
        status: 202,
        body: { id: "j1", kind: "archive", status: "queued" },
      };
    },
  });
  window.history.pushState({}, "", `/accounts/${ACCOUNT_ID}`);
  render(<App />);
  return { calls, posted };
}

beforeEach(async () => {
  await i18n.changeLanguage("en");
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

it("unlocks a locked account", async () => {
  const { posted } = open({ status: "locked" });
  fireEvent.click(
    await screen.findByRole("button", { name: "Unlock the account" }),
  );
  expect(
    await screen.findByText(
      "The agent will unlock the account within a minute.",
    ),
  ).toBeInTheDocument();
  expect(posted.unlock).toEqual({ reason: null });
});

it("archives a mailbox and removes the messages once confirmed", async () => {
  const { posted } = open();
  expect(
    screen.queryByRole("button", { name: "Unlock the account" }),
  ).toBeNull();
  fireEvent.click(
    await screen.findByRole("button", { name: "Archive the mailbox" }),
  );
  const form = screen.getByRole("form", { name: "Archive the mailbox" });
  fireEvent.change(within(form).getByLabelText(/Messages older than/), {
    target: { value: "365" },
  });
  fireEvent.click(
    within(form).getByLabelText(/Delete the messages from the mailbox/),
  );
  const submit = within(form).getByRole("button", {
    name: "Archive, then delete from the mailbox",
  });
  expect(submit).toBeDisabled();
  fireEvent.change(
    within(form).getByLabelText(/To confirm, type the address/),
    {
      target: { value: "H.Badi@lnhc.dz" },
    },
  );
  fireEvent.click(submit);
  await waitFor(() => expect(posted.archive).toBeDefined());
  expect(posted.archive).toEqual({
    note: "",
    remove_from_mailbox: true,
    older_than_days: 365,
    confirm: "H.Badi@lnhc.dz",
  });
  expect(
    await screen.findByText(/will leave the mailbox once archived/),
  ).toBeInTheDocument();
});

it("deletes a mailbox only with a backup and the address typed again", async () => {
  const { posted } = open();
  fireEvent.click(
    await screen.findByRole("button", { name: "Delete the mailbox" }),
  );
  const form = screen.getByRole("form", { name: "Delete the mailbox" });
  expect(
    within(form).getByText(/restorable from the last backup/),
  ).toBeInTheDocument();
  const submit = within(form).getByRole("button", {
    name: "Delete from Carbonio for good",
  });
  expect(submit).toBeDisabled();
  fireEvent.change(
    within(form).getByLabelText(/To confirm, type the address/),
    {
      target: { value: "h.badi@lnhc.dz" },
    },
  );
  fireEvent.change(within(form).getByLabelText("Reason"), {
    target: { value: "départ" },
  });
  fireEvent.click(submit);
  await waitFor(() => expect(posted.delete).toBeDefined());
  expect(posted.delete).toEqual({
    confirm: "h.badi@lnhc.dz",
    reason: "départ",
  });
});

it("refuses to delete a mailbox never backed up, and hides it from others", async () => {
  open({ last_snapshot: null, snapshot_count: 0 });
  fireEvent.click(
    await screen.findByRole("button", { name: "Delete the mailbox" }),
  );
  expect(screen.getByText(/No backup of this mailbox/)).toBeInTheDocument();
  vi.unstubAllGlobals();
});

it("does not offer mailbox management to domain administrators", async () => {
  open({}, user("domain_admin", DOMAIN_ID));
  expect(
    await screen.findByRole("heading", { name: "h.badi@lnhc.dz" }),
  ).toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: "Delete the mailbox" }),
  ).toBeNull();
});

it("shows the password expiry and resets the password", async () => {
  const soon = new Date(Date.now() + 3 * 86_400_000).toISOString();
  const { posted } = open({ password_expires_at: soon });
  expect(await screen.findByText("Password expiry")).toBeInTheDocument();
  expect(screen.getByText("in 3 days")).toHaveClass("warn-text");
  fireEvent.click(screen.getByRole("button", { name: "Password" }));
  const form = screen.getByRole("form", { name: "Password" });
  const submit = within(form).getByRole("button", {
    name: "Reset the password",
  });
  fireEvent.change(within(form).getByLabelText("New password"), {
    target: { value: "Secret-Pass-1" },
  });
  fireEvent.change(within(form).getByLabelText("Confirm"), {
    target: { value: "Secret-Pass-2" },
  });
  expect(within(form).getByText("The two entries differ.")).toBeInTheDocument();
  expect(submit).toBeDisabled();
  // Generated: both fields filled with the same strong password, shown.
  fireEvent.click(within(form).getByRole("button", { name: "Generate" }));
  const generated = (
    within(form).getByLabelText("New password") as HTMLInputElement
  ).value;
  expect(generated).toHaveLength(16);
  expect(generated).toMatch(/[A-Z]/);
  expect(generated).toMatch(/[a-z]/);
  expect(generated).toMatch(/[0-9]/);
  fireEvent.click(submit);
  expect(
    await screen.findByText(
      "New password of h.badi@lnhc.dz: the agent applies it within a minute.",
    ),
  ).toBeInTheDocument();
  expect(posted.password).toEqual({ password: generated, must_change: true });
});
