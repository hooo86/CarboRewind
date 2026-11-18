import { useMemo, useRef, useState } from "react";
import type { FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useTranslation } from "react-i18next";

import { api } from "../api/endpoints";
import type {
  Domain,
  ImportReport,
  MigrationBatchDetail,
  MigrationMailbox,
  MigrationRowInput,
  MigrationSource,
  Page,
} from "../api/types";
import { useAuth } from "../auth/AuthContext";
import {
  LoadView,
  StatusBadge,
  errorMessage,
  useLoad,
} from "../components/common";
import { NoticeBar, type Notice } from "../components/notices";
import { formatBytes, formatDate } from "../utils/format";
import { BatchForm } from "./MigrationsPage";

type Filter = "all" | "unmatched" | "todo" | "failed" | "done";

/** "source[;destination[;password]]" per line (comma or tab also accepted). */
export function parseLines(text: string): MigrationRowInput[] {
  return text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      const [source, target, password] = line
        .split(/[;\t,]/)
        .map((v) => v.trim());
      return {
        source_email: source ?? "",
        target_email: target || null,
        password: password || null,
      };
    });
}

function MatchBadge({ mailbox }: { mailbox: MigrationMailbox }) {
  const { t } = useTranslation();
  if (!mailbox.match)
    return <span className="badge warn">{t("migrations.match.none")}</span>;
  const tone = mailbox.match === "local_part" ? "info" : "ok";
  return (
    <span className={`badge ${tone}`}>
      {t(`migrations.match.${mailbox.match}`)}
    </span>
  );
}

function Progress({ mailbox }: { mailbox: MigrationMailbox }) {
  const { t } = useTranslation();
  const p = mailbox.progress ?? {};
  if (mailbox.status === "running" && !mailbox.dry_run) {
    return (
      <span className="summary">
        {t("migrations.running", {
          copied: Number(p.copied ?? 0),
          folder: String(p.folder ?? "…"),
          done: Number(p.folders_done ?? 0),
          total: Number(p.folders_total ?? 0),
        })}
      </span>
    );
  }
  const s = mailbox.stats;
  const lines: string[] = [];
  if (s?.finished_at) {
    lines.push(
      t("migrations.lastRun", {
        copied: s.copied ?? 0,
        failed: s.failed ?? 0,
        duplicates: s.duplicates ?? 0,
        total: s.total_copied ?? 0,
        size: formatBytes(s.total_bytes ?? s.bytes ?? 0),
      }),
    );
    if (s.cancelled) lines.push(t("migrations.wasCancelled"));
  }
  if (s?.plan) {
    lines.push(
      t("migrations.plan", {
        messages: s.plan.messages,
        folders: s.plan.folders,
      }),
    );
  }
  if (lines.length === 0) return <span className="muted">—</span>;
  return (
    <span className="summary">
      {lines.map((line) => (
        <span key={line} className="block">
          {line}
        </span>
      ))}
    </span>
  );
}

function MailboxEditor({
  mailbox,
  onDone,
}: {
  mailbox: MigrationMailbox;
  onDone: (changed: boolean) => void;
}) {
  const { t } = useTranslation();
  const [target, setTarget] = useState(mailbox.target_email ?? "");
  const [login, setLogin] = useState(mailbox.source_login ?? "");
  const [password, setPassword] = useState("");
  const [root, setRoot] = useState(mailbox.root ?? "");
  const [error, setError] = useState<string | null>(null);
  const [suggestions, setSuggestions] = useState<string[]>([]);

  async function search(q: string) {
    setTarget(q);
    if (q.length < 2) return;
    try {
      const page = await api.accountList({
        q,
        filters: [],
        sort: "email",
        order: "asc",
        limit: 8,
        offset: 0,
      });
      setSuggestions(page.items.map((a) => a.email));
    } catch {
      setSuggestions([]);
    }
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    const body: Parameters<typeof api.updateMigrationMailbox>[1] = {
      source_login: login.trim() || null,
      root: root.trim() || null,
    };
    if (target.trim()) body.target_email = target.trim();
    else body.clear_target = true;
    if (password) body.password = password;
    try {
      await api.updateMigrationMailbox(mailbox.id, body);
      onDone(true);
    } catch (err) {
      setError(t("error", { message: errorMessage(err) }));
    }
  }

  const listId = `targets-${mailbox.id}`;
  return (
    <form className="card stack" onSubmit={(e) => void submit(e)}>
      <h3>{mailbox.source_email}</h3>
      <div className="row">
        <label className="grow">
          {t("migrations.destination")}
          <input
            list={listId}
            value={target}
            placeholder="prenom@nouveau-domaine.com"
            onChange={(e) => void search(e.target.value)}
          />
          <datalist id={listId}>
            {suggestions.map((s) => (
              <option key={s} value={s} />
            ))}
          </datalist>
        </label>
        <label>
          {t("migrations.login")}
          <input
            value={login}
            placeholder={mailbox.source_email}
            autoComplete="off"
            onChange={(e) => setLogin(e.target.value)}
          />
        </label>
        <label>
          {t("migrations.password")}
          <input
            type="password"
            autoComplete="new-password"
            placeholder={
              mailbox.password_set ? t("migrations.passwordKept") : undefined
            }
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </label>
        <label>
          {t("migrations.mailboxRoot")}
          <input
            value={root}
            maxLength={128}
            onChange={(e) => setRoot(e.target.value)}
          />
        </label>
      </div>
      {error && (
        <p role="alert" className="alert error">
          {error}
        </p>
      )}
      <div className="actions">
        <button type="submit">{t("save")}</button>
        <button
          type="button"
          className="secondary"
          onClick={() => onDone(false)}
        >
          {t("cancel")}
        </button>
      </div>
    </form>
  );
}

function ReportView({ report }: { report: ImportReport }) {
  const { t } = useTranslation();
  return (
    <div
      className="card stack"
      role="status"
      aria-label={t("migrations.report")}
    >
      <strong>
        {t("migrations.reportSummary", {
          added: report.added,
          updated: report.updated,
          matched: report.matched,
          unmatched: report.unmatched.length,
          errors: report.errors.length,
        })}
      </strong>
      {report.unmatched.length > 0 && (
        <details open>
          <summary>{t("migrations.unmatchedTitle")}</summary>
          <ul>
            {report.unmatched.map((u) => (
              <li key={`${u.line}-${u.source}`}>
                {u.source}
                {u.candidates.length > 0
                  ? ` — ${t("migrations.candidates", { list: u.candidates.join(", ") })}`
                  : ` — ${t("migrations.noCandidate")}`}
              </li>
            ))}
          </ul>
        </details>
      )}
      {report.errors.length > 0 && (
        <details open>
          <summary className="bad-text">{t("migrations.errorsTitle")}</summary>
          <ul>
            {report.errors.map((e) => (
              <li key={`${e.line}-${e.message}`}>
                {e.line > 0 && t("migrations.line", { line: e.line })}{" "}
                {e.message}
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

function AddPanel({
  batchId,
  domains,
  onAdded,
}: {
  batchId: string;
  domains: Domain[];
  onAdded: (report: ImportReport) => void;
}) {
  const { t } = useTranslation();
  const [domain, setDomain] = useState("");
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const file = useRef<HTMLInputElement>(null);

  async function run(action: () => Promise<ImportReport>) {
    setBusy(true);
    setError(null);
    try {
      onAdded(await action());
    } catch (err) {
      setError(t("error", { message: errorMessage(err) }));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="card stack" aria-labelledby="add-title">
      <h2 id="add-title">{t("migrations.addTitle")}</h2>
      <label>
        {t("migrations.targetDomain")}
        <select value={domain} onChange={(e) => setDomain(e.target.value)}>
          <option value="">{t("migrations.anyDomain")}</option>
          {domains.map((d) => (
            <option key={d.id} value={d.name}>
              {d.name}
            </option>
          ))}
        </select>
      </label>
      <p className="muted">{t("migrations.matchHelp")}</p>
      <div className="split">
        <div className="stack">
          <h3>{t("migrations.importTitle")}</h3>
          <p className="muted">
            {t("migrations.importHelp")}{" "}
            <a href="/api/migrations/template?format=xlsx">
              {t("migrations.templateXlsx")}
            </a>{" "}
            ·{" "}
            <a href="/api/migrations/template?format=csv">
              {t("migrations.templateCsv")}
            </a>
          </p>
          <label>
            {t("migrations.file")}
            <input
              ref={file}
              type="file"
              accept=".xlsx,.csv,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            />
          </label>
          <button
            type="button"
            disabled={busy}
            onClick={() => {
              const chosen = file.current?.files?.[0];
              if (!chosen) {
                setError(t("migrations.chooseFile"));
                return;
              }
              void run(async () => {
                const report = await api.importMigrationFile(
                  batchId,
                  chosen,
                  domain || undefined,
                );
                if (file.current) file.current.value = "";
                return report;
              });
            }}
          >
            {t("migrations.import")}
          </button>
        </div>
        <form
          className="stack"
          onSubmit={(e) => {
            e.preventDefault();
            void run(async () => {
              const report = await api.addMigrationMailboxes(
                batchId,
                parseLines(text),
                domain || undefined,
              );
              setText("");
              return report;
            });
          }}
        >
          <h3>{t("migrations.manualTitle")}</h3>
          <label>
            {t("migrations.manualHelp")}
            <textarea
              rows={5}
              value={text}
              spellCheck={false}
              placeholder={
                "ana@ancien.com\nben@ancien.com;benoit@nouveau.com;MotDePasse"
              }
              onChange={(e) => setText(e.target.value)}
            />
          </label>
          <button type="submit" disabled={busy || !text.trim()}>
            {t("migrations.add")}
          </button>
        </form>
      </div>
      {error && (
        <p role="alert" className="alert error">
          {error}
        </p>
      )}
      <div className="actions">
        <button
          type="button"
          className="secondary"
          disabled={busy}
          onClick={() =>
            void run(() => api.rematchMigration(batchId, domain || undefined))
          }
        >
          {t("migrations.rematch")}
        </button>
      </div>
    </section>
  );
}

const FILTERS: Filter[] = ["all", "unmatched", "todo", "failed", "done"];

function keep(filter: Filter, m: MigrationMailbox): boolean {
  switch (filter) {
    case "unmatched":
      return m.target_account_id === null;
    case "todo":
      return m.status === "new" || !!m.dry_run || m.status === "cancelled";
    case "failed":
      return m.status === "failed" || (m.stats?.failed ?? 0) > 0;
    case "done":
      return m.status === "succeeded" && !m.dry_run;
    default:
      return true;
  }
}

export function MigrationBatchPage() {
  const { id = "" } = useParams();
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { canManageMigrations } = useAuth();
  const [batch, reload] = useLoad<MigrationBatchDetail>(
    () => api.migrationBatch(id),
    [id],
    5000,
  );
  const [sources] = useLoad<MigrationSource[]>(
    () => api.migrationSources(),
    [],
  );
  const [domains] = useLoad<Page<Domain>>(() => api.domains(), []);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [filter, setFilter] = useState<Filter>("all");
  const [editing, setEditing] = useState<string | null>(null);
  const [editBatch, setEditBatch] = useState(false);
  const [report, setReport] = useState<ImportReport | null>(null);
  const [notice, setNotice] = useState<Notice>(null);
  const [busy, setBusy] = useState(false);

  const visible = useMemo(
    () =>
      batch.state === "ready"
        ? batch.data.mailboxes.filter((m) => keep(filter, m))
        : [],
    [batch, filter],
  );

  if (!canManageMigrations) {
    return <p className="alert warn">{t("settings.forbidden")}</p>;
  }

  async function act(action: () => Promise<string | null>) {
    setBusy(true);
    setNotice(null);
    try {
      const text = await action();
      if (text) setNotice({ kind: "ok", text });
      reload();
    } catch (err) {
      setNotice({
        kind: "error",
        text: t("error", { message: errorMessage(err) }),
      });
    } finally {
      setBusy(false);
    }
  }

  const start = (dryRun: boolean) =>
    act(async () => {
      const ids = selected.size ? [...selected] : null;
      const r = await api.startMigration(id, ids, dryRun);
      const skipped = r.skipped.length
        ? " " +
          t("migrations.skipped", {
            count: r.skipped.length,
            list: r.skipped
              .slice(0, 5)
              .map(
                (s) =>
                  `${s.source} (${t(`migrations.reason.${s.reason}`, { defaultValue: s.reason })})`,
              )
              .join(", "),
          })
        : "";
      return (
        t(dryRun ? "migrations.testsQueued" : "migrations.copiesQueued", {
          count: r.queued,
        }) + skipped
      );
    });

  return (
    <>
      <p>
        <Link to="/migrations">← {t("migrations.title")}</Link>
      </p>
      <LoadView load={batch} reload={reload}>
        {(b) => {
          const allVisible =
            visible.length > 0 && visible.every((m) => selected.has(m.id));
          const toggleAll = () =>
            setSelected(
              allVisible ? new Set() : new Set(visible.map((m) => m.id)),
            );
          const toggle = (mid: string) =>
            setSelected((prev) => {
              const next = new Set(prev);
              if (next.has(mid)) next.delete(mid);
              else next.add(mid);
              return next;
            });
          const active = (b.counts.running ?? 0) + (b.counts.queued ?? 0);
          return (
            <>
              <div className="toolbar">
                <h1 className="grow">{b.name}</h1>
                <button
                  type="button"
                  className="secondary"
                  onClick={() => setEditBatch((v) => !v)}
                >
                  {t("migrations.options")}
                </button>
                <button
                  type="button"
                  className="secondary"
                  disabled={busy || active > 0}
                  onClick={() => {
                    if (
                      window.confirm(
                        t("migrations.confirmDeleteBatch", { name: b.name }),
                      )
                    )
                      void act(async () => {
                        await api.deleteMigrationBatch(b.id);
                        navigate("/migrations");
                        return null;
                      });
                  }}
                >
                  {t("delete")}
                </button>
              </div>
              <p className="muted">
                {t("migrations.batchInfo", {
                  source: b.source_name,
                  auth: t(`migrations.auth.${b.auth_mode}`),
                })}
                {b.options.root && (
                  <> · {t("migrations.rootIs", { root: b.options.root })}</>
                )}
                {!!b.options.exclude_folders?.length && (
                  <>
                    {" "}
                    ·{" "}
                    {t("migrations.excludeIs", {
                      list: b.options.exclude_folders.join(", "),
                    })}
                  </>
                )}
                {(b.options.since || b.options.before) && (
                  <>
                    {" · "}
                    {t("migrations.rangeIs", {
                      since: b.options.since ?? "…",
                      before: b.options.before ?? "…",
                    })}
                  </>
                )}
              </p>
              {editBatch && sources.state === "ready" && (
                <BatchForm
                  sources={sources.data}
                  initial={b}
                  onSaved={() => {
                    setEditBatch(false);
                    reload();
                  }}
                  onCancel={() => setEditBatch(false)}
                />
              )}
              <NoticeBar notice={notice} />
              <AddPanel
                batchId={b.id}
                domains={domains.state === "ready" ? domains.data.items : []}
                onAdded={(r) => {
                  setReport(r);
                  reload();
                }}
              />
              {report && <ReportView report={report} />}

              <h2>
                {t("migrations.mailboxes", { count: b.counts.mailboxes ?? 0 })}
              </h2>
              <div
                className="toolbar"
                role="group"
                aria-label={t("migrations.filter")}
              >
                {FILTERS.map((f) => (
                  <button
                    key={f}
                    type="button"
                    className={`small ${filter === f ? "" : "secondary"}`}
                    aria-pressed={filter === f}
                    onClick={() => setFilter(f)}
                  >
                    {t(`migrations.filters.${f}`)}
                  </button>
                ))}
              </div>
              <div
                className="selection-bar"
                role="region"
                aria-label={t("migrations.actions")}
              >
                <strong>
                  {selected.size
                    ? t("migrations.selected", { count: selected.size })
                    : t("migrations.allMailboxes")}
                </strong>
                <button
                  type="button"
                  className="small secondary"
                  disabled={busy}
                  onClick={() => void start(true)}
                >
                  {t("migrations.testConnection")}
                </button>
                <button
                  type="button"
                  className="small"
                  disabled={busy}
                  onClick={() => void start(false)}
                >
                  {t("migrations.startCopy")}
                </button>
                <button
                  type="button"
                  className="small secondary"
                  disabled={busy || active === 0}
                  onClick={() =>
                    void act(async () =>
                      t("migrations.cancelled", {
                        count: (await api.cancelMigration(b.id)).cancelled,
                      }),
                    )
                  }
                >
                  {t("migrations.cancel")}
                </button>
                {selected.size > 0 && (
                  <button
                    type="button"
                    className="small secondary"
                    disabled={busy}
                    onClick={() => {
                      if (
                        !window.confirm(
                          t("migrations.confirmRemove", {
                            count: selected.size,
                          }),
                        )
                      )
                        return;
                      void act(async () => {
                        for (const mid of selected)
                          await api.deleteMigrationMailbox(mid);
                        setSelected(new Set());
                        return null;
                      });
                    }}
                  >
                    {t("migrations.remove")}
                  </button>
                )}
                <button
                  type="button"
                  className="small secondary"
                  disabled={busy}
                  onClick={() => {
                    if (window.confirm(t("migrations.confirmForget")))
                      void act(async () => {
                        await api.forgetMigrationPasswords(b.id);
                        return t("migrations.forgotten");
                      });
                  }}
                >
                  {t("migrations.forgetPasswords")}
                </button>
              </div>
              {visible.length === 0 ? (
                <p className="muted">{t("migrations.noMailbox")}</p>
              ) : (
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>
                          <input
                            type="checkbox"
                            aria-label={t("accounts.selectPage")}
                            checked={allVisible}
                            onChange={toggleAll}
                          />
                        </th>
                        <th>{t("migrations.sourceMailbox")}</th>
                        <th>{t("migrations.destination")}</th>
                        <th>{t("migrations.matchTitle")}</th>
                        <th>{t("migrations.password")}</th>
                        <th>{t("jobs.status")}</th>
                        <th>{t("migrations.progress")}</th>
                        <th />
                      </tr>
                    </thead>
                    <tbody>
                      {visible.map((m) => (
                        <tr
                          key={m.id}
                          className={
                            selected.has(m.id) ? "selected" : undefined
                          }
                        >
                          <td>
                            <input
                              type="checkbox"
                              aria-label={t("accounts.selectOne", {
                                email: m.source_email,
                              })}
                              checked={selected.has(m.id)}
                              onChange={() => toggle(m.id)}
                            />
                          </td>
                          <td>
                            {m.source_email}
                            {m.source_login && (
                              <span className="muted block">
                                {t("migrations.loginIs", {
                                  login: m.source_login,
                                })}
                              </span>
                            )}
                          </td>
                          <td>
                            {m.target_email && m.target_account_id ? (
                              <Link to={`/accounts/${m.target_account_id}`}>
                                {m.target_email}
                              </Link>
                            ) : (
                              <span className="warn-text">
                                {t("migrations.noDestination")}
                              </span>
                            )}
                            {m.root && (
                              <span className="muted block">→ /{m.root}</span>
                            )}
                          </td>
                          <td>
                            <MatchBadge mailbox={m} />
                          </td>
                          <td>
                            {m.password_set ? (
                              <span className="badge ok">
                                {t("migrations.passwordSet")}
                              </span>
                            ) : b.auth_mode === "password" ? (
                              <span className="badge warn">
                                {t("migrations.passwordMissing")}
                              </span>
                            ) : (
                              <span className="muted">
                                {t("migrations.masterUsed")}
                              </span>
                            )}
                          </td>
                          <td>
                            <StatusBadge status={m.status} />
                            {m.dry_run && (
                              <span className="muted block">
                                {t("migrations.test")}
                              </span>
                            )}
                            {m.error && (
                              <span className="bad-text block" title={m.error}>
                                {m.error}
                              </span>
                            )}
                          </td>
                          <td>
                            <Progress mailbox={m} />
                            {m.last_run_at && (
                              <span className="muted block">
                                {formatDate(m.last_run_at)}
                              </span>
                            )}
                          </td>
                          <td>
                            <button
                              type="button"
                              className="small secondary"
                              disabled={
                                m.status === "queued" || m.status === "running"
                              }
                              onClick={() =>
                                setEditing(editing === m.id ? null : m.id)
                              }
                            >
                              {t("edit")}
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              {editing &&
                b.mailboxes
                  .filter((m) => m.id === editing)
                  .map((m) => (
                    <MailboxEditor
                      key={m.id}
                      mailbox={m}
                      onDone={(changed) => {
                        setEditing(null);
                        if (changed) reload();
                      }}
                    />
                  ))}
            </>
          );
        }}
      </LoadView>
    </>
  );
}
