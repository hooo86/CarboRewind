import { useState } from "react";
import type { FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";

import { api } from "../api/endpoints";
import type {
  ImapSecurity,
  MigrationAuth,
  MigrationBatch,
  MigrationOptions,
  MigrationSource,
  MigrationSourceInput,
} from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { LoadView, errorMessage, useLoad } from "../components/common";
import { NoticeBar, type Notice } from "../components/notices";
import { formatDate } from "../utils/format";

const DEFAULT_PORT: Record<ImapSecurity, number> = {
  ssl: 993,
  starttls: 143,
  none: 143,
};

function SourceForm({
  initial,
  onDone,
}: {
  initial: MigrationSource | null;
  onDone: () => void;
}) {
  const { t } = useTranslation();
  const [form, setForm] = useState<MigrationSourceInput>(() => ({
    name: initial?.name ?? "",
    host: initial?.host ?? "",
    port: initial?.port ?? 993,
    security: initial?.security ?? "ssl",
    verify_tls: initial?.verify_tls ?? true,
    auth_mode: initial?.auth_mode ?? "password",
    master_username: initial?.master_username ?? "",
    master_separator: initial?.master_separator ?? "*",
  }));
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const set = <K extends keyof MigrationSourceInput>(
    key: K,
    value: MigrationSourceInput[K],
  ) => setForm((f) => ({ ...f, [key]: value }));
  const master = form.auth_mode !== "password";

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    const body: MigrationSourceInput = {
      ...form,
      master_username: master ? form.master_username : null,
      // Empty: keep the stored password (write-only field).
      master_password: master && password ? password : null,
    };
    try {
      if (initial) await api.updateMigrationSource(initial.id, body);
      else await api.createMigrationSource(body);
      onDone();
    } catch (err) {
      setError(t("error", { message: errorMessage(err) }));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="card stack" onSubmit={(e) => void submit(e)}>
      <h3>
        {initial ? t("migrations.editSource") : t("migrations.newSource")}
      </h3>
      <div className="row">
        <label>
          {t("migrations.sourceName")}
          <input
            required
            maxLength={255}
            value={form.name}
            onChange={(e) => set("name", e.target.value)}
          />
        </label>
        <label className="grow">
          {t("migrations.host")}
          <input
            required
            maxLength={253}
            placeholder="imap.ancien-domaine.com"
            value={form.host}
            onChange={(e) => set("host", e.target.value)}
          />
        </label>
        <label>
          {t("migrations.security")}
          <select
            value={form.security}
            onChange={(e) => {
              const security = e.target.value as ImapSecurity;
              setForm((f) => ({
                ...f,
                security,
                port: DEFAULT_PORT[security],
              }));
            }}
          >
            <option value="ssl">SSL/TLS</option>
            <option value="starttls">STARTTLS</option>
            <option value="none">{t("migrations.noEncryption")}</option>
          </select>
        </label>
        <label>
          {t("migrations.port")}
          <input
            type="number"
            min={1}
            max={65535}
            value={form.port}
            onChange={(e) => set("port", Number(e.target.value))}
          />
        </label>
      </div>
      {form.security !== "none" && (
        <label className="inline">
          <input
            type="checkbox"
            checked={form.verify_tls}
            onChange={(e) => set("verify_tls", e.target.checked)}
          />
          {t("migrations.verifyTls")}
        </label>
      )}
      {(form.security === "none" || !form.verify_tls) && (
        <p className="alert warn">{t("migrations.insecure")}</p>
      )}
      <label>
        {t("migrations.authMode")}
        <select
          value={form.auth_mode}
          onChange={(e) => set("auth_mode", e.target.value as MigrationAuth)}
        >
          {(["password", "master_plain", "master_login"] as const).map((m) => (
            <option key={m} value={m}>
              {t(`migrations.auth.${m}`)}
            </option>
          ))}
        </select>
      </label>
      <p className="muted">{t(`migrations.authHelp.${form.auth_mode}`)}</p>
      {master && (
        <div className="row">
          <label className="grow">
            {t("migrations.masterUser")}
            <input
              required
              maxLength={320}
              autoComplete="off"
              value={form.master_username ?? ""}
              onChange={(e) => set("master_username", e.target.value)}
            />
          </label>
          <label className="grow">
            {t("migrations.masterPassword")}
            <input
              type="password"
              autoComplete="new-password"
              required={!initial?.master_password_set}
              placeholder={
                initial?.master_password_set
                  ? t("migrations.passwordKept")
                  : undefined
              }
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </label>
          {form.auth_mode === "master_login" && (
            <label>
              {t("migrations.separator")}
              <input
                required
                maxLength={8}
                size={3}
                value={form.master_separator}
                onChange={(e) => set("master_separator", e.target.value)}
              />
            </label>
          )}
        </div>
      )}
      {error && (
        <p role="alert" className="alert error">
          {error}
        </p>
      )}
      <div className="actions">
        <button type="submit" disabled={busy}>
          {t("save")}
        </button>
        <button type="button" className="secondary" onClick={onDone}>
          {t("cancel")}
        </button>
      </div>
    </form>
  );
}

export function optionsFromForm(form: {
  root: string;
  exclude: string;
  since: string;
  before: string;
}): MigrationOptions {
  return {
    root: form.root.trim() || null,
    exclude_folders: form.exclude
      .split(/[,\n]/)
      .map((v) => v.trim())
      .filter(Boolean),
    since: form.since || null,
    before: form.before || null,
  };
}

export function BatchForm({
  sources,
  initial,
  onSaved,
  onCancel,
}: {
  sources: MigrationSource[];
  initial: MigrationBatch | null;
  onSaved: (batch: MigrationBatch) => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  const [name, setName] = useState(initial?.name ?? "");
  const [sourceId, setSourceId] = useState(
    initial?.source_id ?? sources[0]?.id ?? "",
  );
  const [form, setForm] = useState({
    root: initial?.options.root ?? "",
    exclude: (initial?.options.exclude_folders ?? []).join(", "),
    since: initial?.options.since ?? "",
    before: initial?.options.before ?? "",
  });
  const [error, setError] = useState<string | null>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    try {
      onSaved(
        await api.saveMigrationBatch(initial?.id ?? null, {
          name,
          source_id: sourceId,
          options: optionsFromForm(form),
        }),
      );
    } catch (err) {
      setError(t("error", { message: errorMessage(err) }));
    }
  }

  return (
    <form className="card stack" onSubmit={(e) => void submit(e)}>
      <h3>{initial ? t("migrations.editBatch") : t("migrations.newBatch")}</h3>
      <div className="row">
        <label className="grow">
          {t("migrations.batchName")}
          <input
            required
            maxLength={255}
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        </label>
        <label>
          {t("migrations.source")}
          <select
            required
            value={sourceId}
            onChange={(e) => setSourceId(e.target.value)}
          >
            {sources.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name} ({s.host})
              </option>
            ))}
          </select>
        </label>
      </div>
      <div className="row">
        <label className="grow">
          {t("migrations.root")}
          <input
            maxLength={128}
            placeholder={t("migrations.rootPlaceholder")}
            value={form.root}
            onChange={(e) => setForm({ ...form, root: e.target.value })}
          />
        </label>
        <label className="grow">
          {t("migrations.exclude")}
          <input
            placeholder="Trash, Junk"
            value={form.exclude}
            onChange={(e) => setForm({ ...form, exclude: e.target.value })}
          />
        </label>
        <label>
          {t("migrations.since")}
          <input
            type="date"
            value={form.since}
            onChange={(e) => setForm({ ...form, since: e.target.value })}
          />
        </label>
        <label>
          {t("migrations.before")}
          <input
            type="date"
            value={form.before}
            onChange={(e) => setForm({ ...form, before: e.target.value })}
          />
        </label>
      </div>
      {error && (
        <p role="alert" className="alert error">
          {error}
        </p>
      )}
      <div className="actions">
        <button type="submit" disabled={!sourceId}>
          {t("save")}
        </button>
        <button type="button" className="secondary" onClick={onCancel}>
          {t("cancel")}
        </button>
      </div>
    </form>
  );
}

function BatchCounts({ batch }: { batch: MigrationBatch }) {
  const { t } = useTranslation();
  const c = batch.counts;
  return (
    <span className="summary">
      {t("migrations.counts", {
        mailboxes: c.mailboxes ?? 0,
        matched: c.matched ?? 0,
        succeeded: c.succeeded ?? 0,
        failed: c.failed ?? 0,
      })}
      {(c.running ?? 0) + (c.queued ?? 0) > 0 && (
        <>
          {" · "}
          <span className="badge info">
            {t("migrations.inProgress", {
              count: (c.running ?? 0) + (c.queued ?? 0),
            })}
          </span>
        </>
      )}
    </span>
  );
}

export function MigrationsPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { canManageMigrations } = useAuth();
  const [sources, reloadSources] = useLoad<MigrationSource[]>(
    () => api.migrationSources(),
    [],
  );
  const [batches, reloadBatches] = useLoad<MigrationBatch[]>(
    () => api.migrationBatches(),
    [],
    10000,
  );
  const [editing, setEditing] = useState<MigrationSource | "new" | null>(null);
  const [newBatch, setNewBatch] = useState(false);
  const [notice, setNotice] = useState<Notice>(null);

  if (!canManageMigrations) {
    return (
      <>
        <h1>{t("migrations.title")}</h1>
        <p className="alert warn">{t("settings.forbidden")}</p>
      </>
    );
  }

  async function removeSource(source: MigrationSource) {
    if (
      !window.confirm(
        t("migrations.confirmDeleteSource", { name: source.name }),
      )
    )
      return;
    try {
      await api.deleteMigrationSource(source.id);
      reloadSources();
    } catch (err) {
      setNotice({
        kind: "error",
        text: t("error", { message: errorMessage(err) }),
      });
    }
  }

  return (
    <>
      <h1>{t("migrations.title")}</h1>
      <p className="muted">{t("migrations.intro")}</p>
      <NoticeBar notice={notice} />

      <section aria-labelledby="sources-title">
        <div className="toolbar">
          <h2 id="sources-title" className="grow">
            {t("migrations.sources")}
          </h2>
          <button type="button" onClick={() => setEditing("new")}>
            {t("migrations.newSource")}
          </button>
        </div>
        {editing && (
          <SourceForm
            initial={editing === "new" ? null : editing}
            onDone={() => {
              setEditing(null);
              reloadSources();
            }}
          />
        )}
        <LoadView load={sources} reload={reloadSources}>
          {(list) =>
            list.length === 0 ? (
              <p className="muted">{t("migrations.noSource")}</p>
            ) : (
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>{t("migrations.sourceName")}</th>
                      <th>{t("migrations.host")}</th>
                      <th>{t("migrations.authMode")}</th>
                      <th className="num">{t("migrations.batches")}</th>
                      <th />
                    </tr>
                  </thead>
                  <tbody>
                    {list.map((s) => (
                      <tr key={s.id}>
                        <td>{s.name}</td>
                        <td>
                          <code>
                            {s.host}:{s.port}
                          </code>{" "}
                          {s.security === "none" ? (
                            <span className="badge warn">
                              {t("migrations.noEncryption")}
                            </span>
                          ) : (
                            <span className="badge muted">
                              {s.security.toUpperCase()}
                            </span>
                          )}
                          {s.security !== "none" && !s.verify_tls && (
                            <span className="badge warn">
                              {t("migrations.unverified")}
                            </span>
                          )}
                        </td>
                        <td>
                          {t(`migrations.auth.${s.auth_mode}`)}
                          {s.master_username && (
                            <>
                              {" "}
                              (<code>{s.master_username}</code>)
                            </>
                          )}
                        </td>
                        <td className="num">{s.batches}</td>
                        <td className="actions">
                          <button
                            type="button"
                            className="small secondary"
                            onClick={() => setEditing(s)}
                          >
                            {t("edit")}
                          </button>
                          <button
                            type="button"
                            className="small secondary"
                            disabled={s.batches > 0}
                            onClick={() => void removeSource(s)}
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
      </section>

      <section aria-labelledby="batches-title">
        <div className="toolbar">
          <h2 id="batches-title" className="grow">
            {t("migrations.batchesTitle")}
          </h2>
          <button
            type="button"
            disabled={sources.state !== "ready" || sources.data.length === 0}
            onClick={() => setNewBatch(true)}
          >
            {t("migrations.newBatch")}
          </button>
        </div>
        {newBatch && sources.state === "ready" && (
          <BatchForm
            sources={sources.data}
            initial={null}
            onSaved={(b) => navigate(`/migrations/${b.id}`)}
            onCancel={() => setNewBatch(false)}
          />
        )}
        <LoadView load={batches} reload={reloadBatches}>
          {(list) =>
            list.length === 0 ? (
              <p className="muted">{t("migrations.noBatch")}</p>
            ) : (
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>{t("migrations.batchName")}</th>
                      <th>{t("migrations.source")}</th>
                      <th>{t("migrations.progress")}</th>
                      <th>{t("migrations.created")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {list.map((b) => (
                      <tr key={b.id}>
                        <td>
                          <Link to={`/migrations/${b.id}`}>{b.name}</Link>
                        </td>
                        <td>{b.source_name}</td>
                        <td>
                          <BatchCounts batch={b} />
                        </td>
                        <td>{formatDate(b.created_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )
          }
        </LoadView>
      </section>
    </>
  );
}
