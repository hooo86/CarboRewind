import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { useTranslation } from "react-i18next";

import { api } from "../api/endpoints";
import type {
  ArchivedMessage,
  ArchiveMode,
  ArchivePolicy,
  ArchivePolicyInput,
  ArchiveSearch,
  OpenedMessage,
} from "../api/types";
import { useAuth } from "../auth/AuthContext";
import {
  errorMessage,
  LoadView,
  StatusBadge,
  useLoad,
} from "../components/common";
import { NoticeBar, type Notice } from "../components/notices";
import { formatBytes, formatDate } from "../utils/format";

const PAGE = 50;

const EMPTY_POLICY: ArchivePolicyInput = {
  name: "",
  enabled: false,
  mode: "simulate",
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
  timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
  retention_days: 0,
};

/** Account ids <-> e-mail addresses typed by the administrator. */
async function resolveEmails(text: string): Promise<string[]> {
  const emails = text
    .split(/[\s,;]+/)
    .map((e) => e.trim().toLowerCase())
    .filter(Boolean);
  const ids: string[] = [];
  for (const email of emails) {
    const page = await api.accountList({
      q: email,
      filters: [],
      sort: "email",
      order: "asc",
      limit: 20,
      offset: 0,
    });
    const found = page.items.find((a) => a.email.toLowerCase() === email);
    if (!found) throw new Error(email);
    ids.push(found.id);
  }
  return ids;
}

async function emailsOf(ids: string[]): Promise<string> {
  const out: string[] = [];
  for (const id of ids) {
    try {
      out.push((await api.account(id)).email);
    } catch {
      out.push(id);
    }
  }
  return out.join("\n");
}

// ------------------------------------------------------------------ message viewer

function MessageViewer({
  message,
  onClose,
}: {
  message: ArchivedMessage;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const [jobId, setJobId] = useState<string | null>(null);
  const [opened, setOpened] = useState<OpenedMessage | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    setOpened(null);
    setError(null);
    api
      .openArchivedMessage(message.id)
      .then(({ job_id }) => {
        if (cancelled) return;
        setJobId(job_id);
        const poll = () => {
          api
            .openedMessage(job_id)
            .then((r) => {
              if (cancelled) return;
              if (r.status === "succeeded" || r.status === "failed") {
                setOpened(r);
                if (r.status === "failed")
                  setError(r.error ?? t("archives.readFailed"));
              } else timer = window.setTimeout(poll, 1500);
            })
            .catch((err: unknown) => !cancelled && setError(errorMessage(err)));
        };
        poll();
      })
      .catch((err: unknown) => !cancelled && setError(errorMessage(err)));
    return () => {
      cancelled = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [message.id, t]);

  const m = opened?.message;
  return (
    <section
      className="card stack viewer"
      aria-label={t("archives.viewer")}
      role="dialog"
    >
      <div className="toolbar">
        <h2 className="grow">{message.subject ?? t("archives.noSubject")}</h2>
        {jobId && m && (
          <a
            className="button secondary small"
            href={api.emlUrl(jobId)}
            download
          >
            {t("archives.downloadEml")}
          </a>
        )}
        <button type="button" className="secondary small" onClick={onClose}>
          {t("archives.close")}
        </button>
      </div>
      {error && (
        <p role="alert" className="alert error">
          {t("error", { message: error })}
        </p>
      )}
      {!m && !error && <p className="muted">{t("archives.reading")}</p>}
      {m && (
        <>
          <dl className="headers">
            <dt>{t("archives.from")}</dt>
            <dd>{m.from ?? "—"}</dd>
            <dt>{t("archives.to")}</dt>
            <dd>{m.to ?? "—"}</dd>
            {m.cc && (
              <>
                <dt>{t("archives.cc")}</dt>
                <dd>{m.cc}</dd>
              </>
            )}
            <dt>{t("archives.date")}</dt>
            <dd>{m.date ?? formatDate(message.item_date)}</dd>
            <dt>{t("archives.folder")}</dt>
            <dd>
              {message.account_email} — {message.folder_path}
            </dd>
          </dl>
          {m.html_only && <p className="muted">{t("archives.htmlOnly")}</p>}
          <pre className="mail-text">{m.text || t("archives.noText")}</pre>
          {m.truncated && <p className="muted">{t("archives.truncated")}</p>}
          {m.attachments.length > 0 && (
            <>
              <h3>{t("archives.attachments")}</h3>
              <ul>
                {m.attachments.map((a, i) => (
                  <li key={i}>
                    {a.name ?? t("archives.unnamed")} ({a.content_type},{" "}
                    {formatBytes(a.size)})
                  </li>
                ))}
              </ul>
            </>
          )}
        </>
      )}
    </section>
  );
}

// ------------------------------------------------------------------ search

function SearchTab() {
  const { t } = useTranslation();
  const [form, setForm] = useState<ArchiveSearch>({});
  const [search, setSearch] = useState<ArchiveSearch>({});
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [viewing, setViewing] = useState<ArchivedMessage | null>(null);
  const [mode, setMode] = useState<"new_folder" | "original">("new_folder");
  const [target, setTarget] = useState("");
  const [notice, setNotice] = useState<Notice>(null);
  const [load, reload] = useLoad(
    () => api.archivedMessages({ ...search, limit: PAGE, offset }),
    [search, offset],
  );
  const set = (k: keyof ArchiveSearch, v: string) =>
    setForm((f) => ({ ...f, [k]: v }));

  function submit(event: FormEvent) {
    event.preventDefault();
    setOffset(0);
    setSelected(new Set());
    const legal = form.removed === "legal";
    setSearch({
      ...form,
      removed: legal ? undefined : form.removed,
      legal_hold: legal ? "true" : undefined,
      date_from: form.date_from ? `${form.date_from}T00:00:00Z` : undefined,
      date_to: form.date_to ? `${form.date_to}T23:59:59Z` : undefined,
    });
  }

  function toggle(id: number) {
    setSelected((s) => {
      const next = new Set(s);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  async function restore() {
    setNotice(null);
    try {
      const [targetId] = target.trim() ? await resolveEmails(target) : [];
      const jobs = await api.restoreArchived([...selected], mode, targetId);
      setNotice({
        kind: "ok",
        text: t("archives.restoreQueued", {
          count: selected.size,
          jobs: jobs.length,
        }),
      });
      setSelected(new Set());
    } catch (err) {
      setNotice({
        kind: "error",
        text: t("error", { message: errorMessage(err) }),
      });
    }
  }

  return (
    <div className="stack">
      <form
        className="card stack"
        onSubmit={submit}
        aria-label={t("archives.search")}
      >
        <div className="row">
          <label className="grow">
            {t("archives.account")}
            <input
              value={form.account ?? ""}
              onChange={(e) => set("account", e.target.value)}
            />
          </label>
          <label className="grow">
            {t("archives.subject")}
            <input
              value={form.q ?? ""}
              onChange={(e) => set("q", e.target.value)}
            />
          </label>
          <label className="grow">
            {t("archives.from")}
            <input
              value={form.sender ?? ""}
              onChange={(e) => set("sender", e.target.value)}
            />
          </label>
          <label className="grow">
            {t("archives.to")}
            <input
              value={form.recipient ?? ""}
              onChange={(e) => set("recipient", e.target.value)}
            />
          </label>
        </div>
        <div className="row">
          <label className="grow">
            {t("archives.folder")}
            <input
              value={form.folder ?? ""}
              placeholder="/Inbox"
              onChange={(e) => set("folder", e.target.value)}
            />
          </label>
          <label>
            {t("archives.dateFrom")}
            <input
              type="date"
              value={form.date_from ?? ""}
              onChange={(e) => set("date_from", e.target.value)}
            />
          </label>
          <label>
            {t("archives.dateTo")}
            <input
              type="date"
              value={form.date_to ?? ""}
              onChange={(e) => set("date_to", e.target.value)}
            />
          </label>
          <label>
            {t("archives.where")}
            <select
              value={form.removed ?? ""}
              onChange={(e) => set("removed", e.target.value)}
            >
              <option value="">{t("archives.whereAll")}</option>
              <option value="true">{t("archives.whereRemoved")}</option>
              <option value="false">{t("archives.whereKept")}</option>
              <option value="legal">{t("archives.whereLegal")}</option>
            </select>
          </label>
        </div>
        <div className="actions">
          <button type="submit">{t("archives.searchButton")}</button>
        </div>
      </form>

      {viewing && (
        <MessageViewer message={viewing} onClose={() => setViewing(null)} />
      )}

      <LoadView load={load} reload={reload}>
        {(page) => (
          <section className="stack">
            <p className="muted">
              {t("archives.results", { count: page.total })}
            </p>
            {page.items.length > 0 && (
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>
                        <span className="sr-only">{t("archives.select")}</span>
                      </th>
                      <th>{t("archives.date")}</th>
                      <th>{t("archives.account")}</th>
                      <th>{t("archives.from")}</th>
                      <th>{t("archives.subject")}</th>
                      <th>{t("archives.folder")}</th>
                      <th>{t("archives.size")}</th>
                      <th>{t("archives.state")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {page.items.map((m) => (
                      <tr key={m.id}>
                        <td>
                          <input
                            type="checkbox"
                            aria-label={t("archives.selectMessage", {
                              subject: m.subject ?? m.id,
                            })}
                            checked={selected.has(m.id)}
                            onChange={() => toggle(m.id)}
                          />
                        </td>
                        <td>{formatDate(m.item_date)}</td>
                        <td>{m.account_email}</td>
                        <td>{m.sender ?? "—"}</td>
                        <td>
                          <button
                            type="button"
                            className="link"
                            onClick={() => setViewing(m)}
                          >
                            {m.subject ?? t("archives.noSubject")}
                          </button>
                          {m.has_attachments && (
                            <span
                              className="muted"
                              title={t("archives.attachments")}
                            >
                              {" "}
                              📎
                            </span>
                          )}
                        </td>
                        <td>{m.folder_path}</td>
                        <td>{formatBytes(m.size)}</td>
                        <td>
                          <span
                            className={`badge ${m.removed_at ? "info" : "muted"}`}
                          >
                            {t(
                              m.removed_at
                                ? "archives.removed"
                                : "archives.inMailbox",
                            )}
                          </span>
                          {m.legal_hold && (
                            <span className="badge warn">
                              {t("archives.legalBadge")}
                            </span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            <div className="actions">
              <button
                type="button"
                className="secondary small"
                disabled={offset === 0}
                onClick={() => setOffset((o) => Math.max(0, o - PAGE))}
              >
                {t("archives.previous")}
              </button>
              <button
                type="button"
                className="secondary small"
                disabled={offset + PAGE >= page.total}
                onClick={() => setOffset((o) => o + PAGE)}
              >
                {t("archives.next")}
              </button>
            </div>
          </section>
        )}
      </LoadView>

      {selected.size > 0 && (
        <section className="card stack" aria-label={t("archives.restoreTitle")}>
          <h2>{t("archives.restoreTitle")}</h2>
          <div className="row">
            <label>
              {t("archives.restoreMode")}
              <select
                value={mode}
                onChange={(e) =>
                  setMode(e.target.value as "new_folder" | "original")
                }
              >
                <option value="new_folder">
                  {t("archives.modeNewFolder")}
                </option>
                <option value="original">{t("archives.modeOriginal")}</option>
              </select>
            </label>
            <label className="grow">
              {t("archives.restoreTarget")}
              <input
                type="email"
                value={target}
                disabled={mode === "original"}
                onChange={(e) => setTarget(e.target.value)}
              />
            </label>
          </div>
          <p className="muted">{t("archives.restoreHelp")}</p>
          <div className="actions">
            <button type="button" onClick={() => void restore()}>
              {t("archives.restoreButton", { count: selected.size })}
            </button>
          </div>
        </section>
      )}
      <NoticeBar notice={notice} />
    </div>
  );
}

// ------------------------------------------------------------------ policies

function PolicyForm({
  policy,
  onDone,
}: {
  policy: ArchivePolicy | null;
  onDone: () => void;
}) {
  const { t } = useTranslation();
  const [form, setForm] = useState<ArchivePolicyInput>(
    policy ? { ...policy } : EMPTY_POLICY,
  );
  const [accounts, setAccounts] = useState("");
  const [excluded, setExcluded] = useState("");
  const [folders, setFolders] = useState(
    (policy?.exclude_folders ?? []).join("\n"),
  );
  const [notice, setNotice] = useState<Notice>(null);
  const domains = useLoad(() => api.domains(), [])[0];
  const set = <K extends keyof ArchivePolicyInput>(
    k: K,
    v: ArchivePolicyInput[K],
  ) => setForm((f) => ({ ...f, [k]: v }));

  useEffect(() => {
    if (!policy) return;
    void emailsOf(policy.account_ids).then(setAccounts);
    void emailsOf(policy.exclude_account_ids).then(setExcluded);
  }, [policy]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setNotice(null);
    try {
      const body: ArchivePolicyInput = {
        ...form,
        account_ids: await resolveEmails(accounts),
        exclude_account_ids: await resolveEmails(excluded),
        exclude_folders: folders
          .split("\n")
          .map((f) => f.trim())
          .filter(Boolean),
      };
      if (policy) await api.updateArchivePolicy(policy.id, body);
      else await api.createArchivePolicy(body);
      onDone();
    } catch (err) {
      setNotice({
        kind: "error",
        text: t("error", { message: errorMessage(err) }),
      });
    }
  }

  return (
    <form
      className="card stack"
      onSubmit={(e) => void submit(e)}
      aria-label={t("archives.policyForm")}
    >
      <h2>{policy ? t("archives.editPolicy") : t("archives.newPolicy")}</h2>
      <div className="row">
        <label className="grow">
          {t("archives.policyName")}
          <input
            required
            value={form.name}
            onChange={(e) => set("name", e.target.value)}
          />
        </label>
        <label>
          {t("archives.mode")}
          <select
            value={form.mode}
            onChange={(e) => set("mode", e.target.value as ArchiveMode)}
          >
            <option value="simulate">{t("archives.modes.simulate")}</option>
            <option value="copy">{t("archives.modes.copy")}</option>
            <option value="archive">{t("archives.modes.archive")}</option>
          </select>
        </label>
      </div>
      <p className="muted">{t(`archives.modeHelp.${form.mode}`)}</p>
      <div className="row">
        <label>
          {t("archives.olderThan")}
          <input
            type="number"
            min={30}
            value={form.older_than_days}
            onChange={(e) => set("older_than_days", Number(e.target.value))}
          />
        </label>
        <label>
          {t("archives.maxPerRun")}
          <input
            type="number"
            min={1}
            max={50000}
            value={form.max_per_run}
            onChange={(e) => set("max_per_run", Number(e.target.value))}
          />
        </label>
        <label>
          {t("archives.retention")}
          <input
            type="number"
            min={0}
            value={form.retention_days}
            onChange={(e) => set("retention_days", Number(e.target.value))}
          />
        </label>
      </div>
      <fieldset>
        <legend>{t("archives.domains")}</legend>
        <LoadView load={domains}>
          {(page) => (
            <div className="row">
              {page.items.map((d) => (
                <label key={d.id} className="inline">
                  <input
                    type="checkbox"
                    checked={form.domain_ids.includes(d.id)}
                    onChange={(e) =>
                      set(
                        "domain_ids",
                        e.target.checked
                          ? [...form.domain_ids, d.id]
                          : form.domain_ids.filter((x) => x !== d.id),
                      )
                    }
                  />
                  {d.name}
                </label>
              ))}
            </div>
          )}
        </LoadView>
        <p className="muted">{t("archives.domainsHelp")}</p>
      </fieldset>
      <div className="row">
        <label className="grow">
          {t("archives.onlyAccounts")}
          <textarea
            rows={3}
            value={accounts}
            onChange={(e) => setAccounts(e.target.value)}
          />
        </label>
        <label className="grow">
          {t("archives.excludedAccounts")}
          <textarea
            rows={3}
            value={excluded}
            onChange={(e) => setExcluded(e.target.value)}
          />
        </label>
        <label className="grow">
          {t("archives.excludedFolders")}
          <textarea
            rows={3}
            value={folders}
            placeholder="/Inbox/Juridique"
            onChange={(e) => setFolders(e.target.value)}
          />
        </label>
      </div>
      <p className="muted">{t("archives.alwaysExcluded")}</p>
      <div className="row">
        <label className="inline">
          <input
            type="checkbox"
            checked={form.keep_flagged}
            onChange={(e) => set("keep_flagged", e.target.checked)}
          />
          {t("archives.keepFlagged")}
        </label>
        <label className="inline">
          <input
            type="checkbox"
            checked={form.keep_unread}
            onChange={(e) => set("keep_unread", e.target.checked)}
          />
          {t("archives.keepUnread")}
        </label>
        <label className="inline">
          <input
            type="checkbox"
            checked={form.include_admins}
            onChange={(e) => set("include_admins", e.target.checked)}
          />
          {t("archives.includeAdmins")}
        </label>
      </div>
      <div className="row">
        <label className="inline">
          <input
            type="checkbox"
            checked={form.enabled}
            onChange={(e) => set("enabled", e.target.checked)}
          />
          {t("archives.enabled")}
        </label>
        <label className="grow">
          {t("archives.schedule")}
          <input
            value={form.schedule}
            onChange={(e) => set("schedule", e.target.value)}
          />
        </label>
        <label>
          {t("monitoring.timezone")}
          <input
            value={form.timezone}
            onChange={(e) => set("timezone", e.target.value)}
          />
        </label>
      </div>
      <NoticeBar notice={notice} />
      <div className="actions">
        <button type="submit">{t("save")}</button>
        <button type="button" className="secondary" onClick={onDone}>
          {t("cancel")}
        </button>
      </div>
    </form>
  );
}

function PoliciesTab() {
  const { t } = useTranslation();
  const [load, reload] = useLoad(() => api.archivePolicies(), []);
  const [editing, setEditing] = useState<ArchivePolicy | "new" | null>(null);
  const [runsOf, setRunsOf] = useState<string | undefined>(undefined);
  const [runs, reloadRuns] = useLoad(
    () => api.archiveRuns(runsOf),
    [runsOf],
    15000,
  );
  const [notice, setNotice] = useState<Notice>(null);

  async function act(p: ArchivePolicy, action: "simulate" | "run" | "delete") {
    setNotice(null);
    try {
      if (action === "delete") {
        if (!window.confirm(t("archives.confirmDelete", { name: p.name })))
          return;
        await api.deleteArchivePolicy(p.id);
      } else {
        if (
          action === "run" &&
          p.mode === "archive" &&
          !window.confirm(t("archives.confirmRun", { name: p.name }))
        )
          return;
        const r = await api.runArchivePolicy(p.id, action === "simulate");
        setNotice({
          kind: "ok",
          text: t("archives.runQueued", {
            count: r.jobs,
            mode: t(`archives.modes.${r.mode}`),
          }),
        });
        setRunsOf(p.id);
      }
      reload();
      reloadRuns();
    } catch (err) {
      setNotice({
        kind: "error",
        text: t("error", { message: errorMessage(err) }),
      });
    }
  }

  return (
    <div className="stack">
      <div className="toolbar">
        <p className="muted grow">{t("archives.policiesHelp")}</p>
        <button type="button" onClick={() => setEditing("new")}>
          {t("archives.newPolicy")}
        </button>
      </div>
      <NoticeBar notice={notice} />
      {editing && (
        <PolicyForm
          policy={editing === "new" ? null : editing}
          onDone={() => {
            setEditing(null);
            reload();
          }}
        />
      )}
      <LoadView load={load} reload={reload}>
        {(policies) =>
          policies.length === 0 ? (
            <p className="muted">{t("archives.noPolicy")}</p>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>{t("archives.policyName")}</th>
                    <th>{t("archives.mode")}</th>
                    <th>{t("archives.olderThan")}</th>
                    <th>{t("archives.schedule")}</th>
                    <th>{t("archives.archivedCol")}</th>
                    <th>{t("archives.lastSimulation")}</th>
                    <th>{t("archives.actions")}</th>
                  </tr>
                </thead>
                <tbody>
                  {policies.map((p) => (
                    <tr key={p.id}>
                      <td>
                        {p.name}{" "}
                        <StatusBadge
                          status={p.enabled ? "enabled" : "disabled"}
                        />
                      </td>
                      <td>
                        {t(`archives.modes.${p.mode}`)}
                        {p.effective_mode !== p.mode && (
                          <p className="warn-text small">
                            {t("archives.simulationNeeded")}
                          </p>
                        )}
                      </td>
                      <td>
                        {t("archives.days", { count: p.older_than_days })}
                        <span className="muted small">
                          {" "}
                          (
                          {t("archives.before", {
                            date: new Date(p.cutoff).toLocaleDateString(),
                          })}
                          )
                        </span>
                      </td>
                      <td>
                        <code>{p.schedule}</code>
                      </td>
                      <td>
                        {p.archived_count} ({formatBytes(p.archived_bytes)})
                      </td>
                      <td>
                        {p.simulated_at ? formatDate(p.simulated_at) : "—"}
                      </td>
                      <td className="actions">
                        <button
                          type="button"
                          className="secondary small"
                          onClick={() => void act(p, "simulate")}
                        >
                          {t("archives.simulate")}
                        </button>
                        {p.mode !== "simulate" && (
                          <button
                            type="button"
                            className="secondary small"
                            disabled={
                              p.mode === "archive" && !p.simulation_valid
                            }
                            onClick={() => void act(p, "run")}
                          >
                            {t("archives.runNow")}
                          </button>
                        )}
                        <button
                          type="button"
                          className="secondary small"
                          onClick={() => setEditing(p)}
                        >
                          {t("edit")}
                        </button>
                        <button
                          type="button"
                          className="secondary small"
                          onClick={() => setRunsOf(p.id)}
                        >
                          {t("archives.history")}
                        </button>
                        <button
                          type="button"
                          className="secondary small danger"
                          onClick={() => void act(p, "delete")}
                        >
                          {t("delete")}
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )
        }
      </LoadView>

      <h2>{t("archives.runs")}</h2>
      <LoadView load={runs} reload={reloadRuns}>
        {(page) =>
          page.items.length === 0 ? (
            <p className="muted">{t("archives.noRun")}</p>
          ) : (
            <div className="table-wrap">
              <table aria-label={t("archives.runs")}>
                <thead>
                  <tr>
                    <th>{t("archives.date")}</th>
                    <th>{t("archives.policyName")}</th>
                    <th>{t("archives.account")}</th>
                    <th>{t("archives.mode")}</th>
                    <th>{t("archives.status")}</th>
                    <th>{t("archives.candidates")}</th>
                    <th>{t("archives.archivedCol")}</th>
                    <th>{t("archives.removedCol")}</th>
                  </tr>
                </thead>
                <tbody>
                  {page.items.map((r) => (
                    <tr key={r.id}>
                      <td>{formatDate(r.finished_at ?? r.created_at)}</td>
                      <td>{r.policy ?? "—"}</td>
                      <td>{r.account_email ?? "—"}</td>
                      <td>{r.mode ? t(`archives.modes.${r.mode}`) : "—"}</td>
                      <td>
                        <StatusBadge status={r.status} />
                        {r.error && <p className="bad-text small">{r.error}</p>}
                      </td>
                      <td>
                        {r.progress?.candidates ?? "—"}
                        {r.progress?.candidate_bytes !== undefined && (
                          <span className="muted">
                            {" "}
                            ({formatBytes(r.progress.candidate_bytes)})
                          </span>
                        )}
                        {r.progress?.truncated && (
                          <span className="muted"> +</span>
                        )}
                      </td>
                      <td>{r.progress?.archived ?? "—"}</td>
                      <td>
                        {r.progress?.removed_count ?? "—"}
                        {!!r.progress?.removed_bytes && (
                          <span className="muted">
                            {" "}
                            ({formatBytes(r.progress.removed_bytes)})
                          </span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )
        }
      </LoadView>
    </div>
  );
}

// ------------------------------------------------------------------ legal hold

function LegalHoldTab() {
  const { t } = useTranslation();
  const [load, reload] = useLoad(() => api.legalHolds(), [], 15000);
  const [email, setEmail] = useState("");
  const [note, setNote] = useState("");
  const [notice, setNotice] = useState<Notice>(null);

  async function start(event: FormEvent) {
    event.preventDefault();
    setNotice(null);
    try {
      const [id] = await resolveEmails(email);
      if (!id) throw new Error(email);
      await api.startLegalHold(id, note);
      setNotice({
        kind: "ok",
        text: t("archives.legalQueued", { email: email.trim() }),
      });
      setEmail("");
      setNote("");
      reload();
    } catch (err) {
      setNotice({
        kind: "error",
        text: t("error", { message: errorMessage(err) }),
      });
    }
  }

  async function release(accountId: string, address: string) {
    if (!window.confirm(t("archives.legalConfirmRelease", { email: address })))
      return;
    setNotice(null);
    try {
      await api.releaseLegalHold(accountId);
      setNotice({ kind: "ok", text: t("archives.legalReleased") });
      reload();
    } catch (err) {
      setNotice({
        kind: "error",
        text: t("error", { message: errorMessage(err) }),
      });
    }
  }

  return (
    <div className="stack">
      <form
        className="card stack"
        onSubmit={(e) => void start(e)}
        aria-label={t("archives.legalTitle")}
      >
        <h2>{t("archives.legalTitle")}</h2>
        <p className="muted">{t("archives.legalHelp")}</p>
        <div className="row">
          <label className="grow">
            {t("archives.legalAccount")}
            <input
              type="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          </label>
          <label className="grow">
            {t("archives.legalNote")}
            <input
              maxLength={500}
              value={note}
              onChange={(e) => setNote(e.target.value)}
            />
          </label>
        </div>
        <div className="actions">
          <button type="submit">{t("archives.legalStart")}</button>
        </div>
      </form>
      <NoticeBar notice={notice} />
      <LoadView load={load} reload={reload}>
        {(holds) =>
          holds.length === 0 ? (
            <p className="muted">{t("archives.legalNone")}</p>
          ) : (
            <div className="table-wrap">
              <table aria-label={t("archives.legalList")}>
                <thead>
                  <tr>
                    <th>{t("archives.account")}</th>
                    <th>{t("archives.messages")}</th>
                    <th>{t("archives.volume")}</th>
                    <th>{t("archives.legalLast")}</th>
                    <th>{t("archives.actions")}</th>
                  </tr>
                </thead>
                <tbody>
                  {holds.map((h) => (
                    <tr key={h.account_id}>
                      <td>
                        {h.account_email}{" "}
                        {h.pending && (
                          <span className="badge info">
                            {t("archives.legalPending")}
                          </span>
                        )}
                      </td>
                      <td className="num">{h.messages}</td>
                      <td className="num">{formatBytes(h.bytes)}</td>
                      <td>{formatDate(h.last_archived_at)}</td>
                      <td className="actions">
                        <button
                          type="button"
                          className="secondary small danger"
                          onClick={() =>
                            void release(h.account_id, h.account_email)
                          }
                        >
                          {t("archives.legalRelease")}
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )
        }
      </LoadView>
    </div>
  );
}

// ------------------------------------------------------------------ page

export function ArchivesPage() {
  const { t } = useTranslation();
  const { canManageSettings } = useAuth();
  const [tab, setTab] = useState<"search" | "policies" | "legal">("search");
  const [summary] = useLoad(() => api.archiveSummary(), []);

  return (
    <>
      <h1>{t("archives.title")}</h1>
      <p className="muted">{t("archives.intro")}</p>
      <LoadView load={summary}>
        {(s) => (
          <section className="tiles" aria-label={t("archives.summary")}>
            <div className="tile">
              <span className="tile-value">{s.messages}</span>
              <span className="tile-label">{t("archives.messages")}</span>
            </div>
            <div className="tile">
              <span className="tile-value">{formatBytes(s.bytes)}</span>
              <span className="tile-label">{t("archives.volume")}</span>
            </div>
            <div className="tile ok">
              <span className="tile-value">{formatBytes(s.removed_bytes)}</span>
              <span className="tile-label">{t("archives.freed")}</span>
            </div>
            <div className="tile">
              <span className="tile-value">{s.accounts}</span>
              <span className="tile-label">{t("archives.accounts")}</span>
            </div>
            <div className={`tile ${s.legal_hold_accounts ? "warn" : ""}`}>
              <span className="tile-value">{s.legal_hold_accounts ?? 0}</span>
              <span className="tile-label">{t("archives.legalTile")}</span>
            </div>
          </section>
        )}
      </LoadView>
      {canManageSettings && (
        <div className="tabs" role="tablist">
          <button
            type="button"
            role="tab"
            aria-selected={tab === "search"}
            className={tab === "search" ? "" : "secondary"}
            onClick={() => setTab("search")}
          >
            {t("archives.search")}
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={tab === "policies"}
            className={tab === "policies" ? "" : "secondary"}
            onClick={() => setTab("policies")}
          >
            {t("archives.policies")}
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={tab === "legal"}
            className={tab === "legal" ? "" : "secondary"}
            onClick={() => setTab("legal")}
          >
            {t("archives.legalTitle")}
          </button>
        </div>
      )}
      {tab === "policies" && canManageSettings ? (
        <PoliciesTab />
      ) : tab === "legal" && canManageSettings ? (
        <LegalHoldTab />
      ) : (
        <SearchTab />
      )}
    </>
  );
}
