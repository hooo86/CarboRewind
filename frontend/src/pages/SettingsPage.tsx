import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { AgentSettingsForm } from "../components/AgentSettingsForm";
import { BlacklistSettingsTab } from "../components/Blacklist";
import { RestoreTestSettingsTab } from "../components/RestoreTests";
import { TelegramSection } from "../components/Telegram";
import { LimitsForm } from "./MailTrafficPage";
import { MonitoringSettingsForm } from "./MonitoringPage";
import { PolicyCard } from "./UsersPage";

import { api } from "../api/endpoints";
import type { NotificationSettings, OutboxEntry } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import {
  LoadView,
  StatusBadge,
  errorMessage,
  useLoad,
} from "../components/common";
import { StorageSettingsSection } from "../components/StorageSettingsForm";
import { formatDate } from "../utils/format";
import { TimezoneSelect } from "../components/TimezoneSelect";

export const EVENTS = [
  "job_failed",
  "backup_partial",
  "restore_failed",
  "agent_offline",
  "daily_digest",
  "migration_done",
  "mail_limit",
  "integrity_failed",
  "replica_lag",
  "storage_full",
  "mailbox_quota",
  "service_down",
  "service_up",
  "mail_queue",
  "archive_report",
  "blacklisted",
  "blacklist_cleared",
  "restore_test_failed",
  "ip_blocked",
];

type Form = Omit<
  NotificationSettings,
  "password_set" | "secret_key_configured" | "fallback_password_set"
> & {
  password: string;
  recipientsText: string;
  fallbackPassword: string;
};

function toForm(s: NotificationSettings): Form {
  return {
    enabled: s.enabled,
    host: s.host,
    port: s.port,
    security: s.security,
    username: s.username,
    sender: s.sender,
    recipients: s.recipients,
    recipientsText: s.recipients.join(", "),
    events: s.events,
    digest_hour: s.digest_hour,
    timezone: s.timezone,
    language: s.language,
    password: "",
    fallback_enabled: s.fallback_enabled ?? false,
    fallback_host: s.fallback_host ?? "",
    fallback_port: s.fallback_port ?? 587,
    fallback_security: s.fallback_security ?? "starttls",
    fallback_username: s.fallback_username ?? "",
    fallback_sender: s.fallback_sender ?? "",
    fallbackPassword: "",
  };
}

function NotificationsForm({
  initial,
  onSaved,
}: {
  initial: NotificationSettings;
  onSaved: (s: NotificationSettings) => void;
}) {
  const { t } = useTranslation();
  const [form, setForm] = useState<Form>(() => toForm(initial));
  const [passwordAction, setPasswordAction] = useState<
    "keep" | "change" | "clear"
  >(initial.password_set ? "keep" : "change");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{
    kind: "ok" | "error";
    text: string;
  } | null>(null);
  const [testTo, setTestTo] = useState("");
  const [fallbackPw, setFallbackPw] = useState<"keep" | "change" | "clear">(
    initial.fallback_password_set ? "keep" : "change",
  );

  useEffect(() => setForm(toForm(initial)), [initial]);

  const set = <K extends keyof Form>(key: K, value: Form[K]) =>
    setForm((f) => ({ ...f, [key]: value }));

  async function save(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setMessage(null);
    const recipients = form.recipientsText
      .split(/[\s,;]+/)
      .map((r) => r.trim())
      .filter(Boolean);
    const { recipientsText: _r, password, fallbackPassword, ...rest } = form;
    void _r;
    try {
      const saved = await api.saveNotificationSettings({
        ...rest,
        recipients,
        password:
          passwordAction === "keep"
            ? null
            : passwordAction === "clear"
              ? ""
              : password,
        fallback_password:
          fallbackPw === "keep"
            ? null
            : fallbackPw === "clear"
              ? ""
              : fallbackPassword,
      });
      setMessage({ kind: "ok", text: t("settings.saved") });
      setPasswordAction(saved.password_set ? "keep" : "change");
      setFallbackPw(saved.fallback_password_set ? "keep" : "change");
      onSaved(saved);
    } catch (err) {
      setMessage({
        kind: "error",
        text: t("error", { message: errorMessage(err) }),
      });
    } finally {
      setBusy(false);
    }
  }

  async function test(server: "main" | "fallback" = "main") {
    setBusy(true);
    setMessage(null);
    try {
      const r = await api.testNotification(testTo.trim() || undefined, server);
      setMessage(
        r.ok
          ? { kind: "ok", text: t("settings.testOk") }
          : {
              kind: "error",
              text: t("settings.testFailed", { error: r.error }),
            },
      );
    } catch (err) {
      setMessage({
        kind: "error",
        text: t("error", { message: errorMessage(err) }),
      });
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="card policy-form" onSubmit={(e) => void save(e)}>
      <h2>{t("settings.notifications")}</h2>
      {!initial.secret_key_configured && (
        <p className="alert warn">{t("settings.noSecretKey")}</p>
      )}
      <label className="check">
        <input
          type="checkbox"
          checked={form.enabled}
          onChange={(e) => set("enabled", e.target.checked)}
        />
        {t("settings.enabled")}
      </label>
      <fieldset>
        <legend>{t("settings.server")}</legend>
        <div className="row">
          <label className="grow">
            {t("settings.host")}
            <input
              value={form.host}
              placeholder="smtp.example.com"
              onChange={(e) => set("host", e.target.value)}
            />
          </label>
          <label>
            {t("settings.port")}
            <input
              type="number"
              min={1}
              max={65535}
              value={form.port}
              onChange={(e) => set("port", Number(e.target.value) || 0)}
            />
          </label>
          <label>
            {t("settings.security")}
            <select
              value={form.security}
              onChange={(e) => {
                const security = e.target.value as Form["security"];
                set("security", security);
                set(
                  "port",
                  security === "ssl" ? 465 : security === "starttls" ? 587 : 25,
                );
              }}
            >
              <option value="starttls">STARTTLS</option>
              <option value="ssl">SSL/TLS</option>
              <option value="none">{t("settings.noEncryption")}</option>
            </select>
          </label>
        </div>
        <div className="row">
          <label className="grow">
            {t("settings.username")}
            <input
              value={form.username}
              autoComplete="off"
              onChange={(e) => set("username", e.target.value)}
            />
          </label>
          <label className="grow">
            {t("settings.password")}
            {passwordAction === "change" ? (
              <input
                type="password"
                autoComplete="new-password"
                value={form.password}
                onChange={(e) => set("password", e.target.value)}
              />
            ) : (
              <span className="muted">
                {passwordAction === "keep"
                  ? t("settings.passwordStored")
                  : t("settings.passwordCleared")}
              </span>
            )}
          </label>
          {initial.password_set && (
            <div className="actions">
              {passwordAction !== "change" && (
                <button
                  type="button"
                  className="small secondary"
                  onClick={() => setPasswordAction("change")}
                >
                  {t("settings.changePassword")}
                </button>
              )}
              {passwordAction !== "clear" && (
                <button
                  type="button"
                  className="small secondary"
                  onClick={() => setPasswordAction("clear")}
                >
                  {t("settings.clearPassword")}
                </button>
              )}
            </div>
          )}
        </div>
      </fieldset>
      <fieldset>
        <legend>{t("settings.fallback")}</legend>
        <label className="check">
          <input
            type="checkbox"
            checked={form.fallback_enabled}
            onChange={(e) => set("fallback_enabled", e.target.checked)}
          />
          {t("settings.fallbackEnabled")}
        </label>
        <p className="muted small">{t("settings.fallbackHelp")}</p>
        <div className="row">
          <label className="grow">
            {t("settings.fallbackHost")}
            <input
              value={form.fallback_host}
              placeholder="smtp.relais.example.com"
              disabled={!form.fallback_enabled}
              onChange={(e) => set("fallback_host", e.target.value)}
            />
          </label>
          <label>
            {t("settings.fallbackPort")}
            <input
              type="number"
              min={1}
              max={65535}
              value={form.fallback_port}
              disabled={!form.fallback_enabled}
              onChange={(e) =>
                set("fallback_port", Number(e.target.value) || 0)
              }
            />
          </label>
          <label>
            {t("settings.fallbackSecurity")}
            <select
              value={form.fallback_security}
              disabled={!form.fallback_enabled}
              onChange={(e) => {
                const security = e.target.value as "starttls" | "ssl" | "none";
                set("fallback_security", security);
                set(
                  "fallback_port",
                  security === "ssl" ? 465 : security === "starttls" ? 587 : 25,
                );
              }}
            >
              <option value="starttls">STARTTLS</option>
              <option value="ssl">SSL/TLS</option>
              <option value="none">{t("settings.noEncryption")}</option>
            </select>
          </label>
        </div>
        <div className="row">
          <label className="grow">
            {t("settings.fallbackUsername")}
            <input
              value={form.fallback_username}
              autoComplete="off"
              disabled={!form.fallback_enabled}
              onChange={(e) => set("fallback_username", e.target.value)}
            />
          </label>
          <label className="grow">
            {t("settings.fallbackPassword")}
            {fallbackPw === "change" ? (
              <input
                type="password"
                autoComplete="new-password"
                value={form.fallbackPassword}
                disabled={!form.fallback_enabled}
                onChange={(e) => set("fallbackPassword", e.target.value)}
              />
            ) : (
              <span className="muted">
                {fallbackPw === "keep"
                  ? t("settings.passwordStored")
                  : t("settings.passwordCleared")}
              </span>
            )}
          </label>
          {initial.fallback_password_set && fallbackPw !== "change" && (
            <div className="actions">
              <button
                type="button"
                className="small secondary"
                onClick={() => setFallbackPw("change")}
              >
                {t("settings.fallbackChangePassword")}
              </button>
            </div>
          )}
          <label className="grow">
            {t("settings.fallbackSender")}
            <input
              type="email"
              value={form.fallback_sender}
              disabled={!form.fallback_enabled}
              onChange={(e) => set("fallback_sender", e.target.value)}
            />
          </label>
        </div>
      </fieldset>
      <fieldset>
        <legend>{t("settings.messages")}</legend>
        <div className="row">
          <label className="grow">
            {t("settings.sender")}
            <input
              type="email"
              value={form.sender}
              placeholder="carborewind@example.com"
              onChange={(e) => set("sender", e.target.value)}
            />
          </label>
          <label>
            {t("settings.language")}
            <select
              value={form.language}
              onChange={(e) =>
                set("language", e.target.value as Form["language"])
              }
            >
              <option value="fr">Français</option>
              <option value="en">English</option>
            </select>
          </label>
        </div>
        <label>
          {t("settings.recipients")}
          <textarea
            rows={2}
            value={form.recipientsText}
            placeholder="admin@example.com, ops@example.com"
            onChange={(e) => set("recipientsText", e.target.value)}
          />
        </label>
      </fieldset>
      <fieldset>
        <legend>{t("settings.events")}</legend>
        {EVENTS.map((ev) => (
          <label key={ev} className="check">
            <input
              type="checkbox"
              checked={form.events[ev] ?? true}
              onChange={(e) =>
                set("events", { ...form.events, [ev]: e.target.checked })
              }
            />
            {t(`settings.eventNames.${ev}`)}
          </label>
        ))}
        <div className="row">
          <label>
            {t("settings.digestHour")}
            <input
              type="number"
              min={0}
              max={23}
              value={form.digest_hour}
              onChange={(e) => set("digest_hour", Number(e.target.value) || 0)}
            />
          </label>
          <label className="grow">
            {t("policies.timezone")}
            <TimezoneSelect
              value={form.timezone}
              onChange={(zone) => set("timezone", zone)}
            />
          </label>
        </div>
      </fieldset>
      {message && (
        <p
          role={message.kind === "error" ? "alert" : "status"}
          className={`alert ${message.kind}`}
        >
          {message.text}
        </p>
      )}
      <div className="actions wrap-actions">
        <button type="submit" disabled={busy}>
          {t("policies.save")}
        </button>
        <input
          type="email"
          aria-label={t("settings.testTo")}
          placeholder={t("settings.testTo")}
          value={testTo}
          onChange={(e) => setTestTo(e.target.value)}
        />
        <button
          type="button"
          className="secondary"
          disabled={busy}
          onClick={() => void test()}
        >
          {t("settings.sendTest")}
        </button>
        {initial.fallback_enabled && initial.fallback_host && (
          <button
            type="button"
            className="secondary"
            disabled={busy}
            onClick={() => void test("fallback")}
          >
            {t("settings.sendTestFallback")}
          </button>
        )}
      </div>
    </form>
  );
}

function Outbox({ refresh }: { refresh: number }) {
  const { t } = useTranslation();
  const [outbox, reload] = useLoad<OutboxEntry[]>(
    () => api.notificationOutbox(),
    [refresh],
    15000,
  );
  return (
    <>
      <h2>{t("settings.outbox")}</h2>
      <LoadView load={outbox} reload={reload}>
        {(rows) =>
          rows.length === 0 ? (
            <p className="muted">{t("settings.outboxEmpty")}</p>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>{t("jobs.created")}</th>
                    <th>{t("settings.subject")}</th>
                    <th>{t("jobs.status")}</th>
                    <th className="num">{t("jobs.attempts")}</th>
                    <th>{t("jobs.error")}</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((n) => (
                    <tr key={n.id}>
                      <td>{formatDate(n.created_at)}</td>
                      <td className="wrap">
                        {n.channel === "telegram" && (
                          <span className="badge info">Telegram</span>
                        )}{" "}
                        {n.subject}
                      </td>
                      <td>
                        <StatusBadge
                          status={
                            n.status === "sent"
                              ? "succeeded"
                              : n.status === "pending"
                                ? "queued"
                                : "failed"
                          }
                        />
                      </td>
                      <td className="num">{n.attempts}</td>
                      <td className="error-cell" title={n.error ?? undefined}>
                        {n.error ?? ""}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )
        }
      </LoadView>
    </>
  );
}

function MonitoringSettingsTab() {
  const [load, reload] = useLoad(() => api.monitoring(), []);
  return (
    <LoadView load={load} reload={reload}>
      {(d) => <MonitoringSettingsForm initial={d.settings} onSaved={reload} />}
    </LoadView>
  );
}

function AgentTab() {
  const [load, reload] = useLoad(() => api.agentTuning(), []);
  return (
    <LoadView load={load} reload={reload}>
      {(cfg) => (
        <AgentSettingsForm key={cfg.revision} initial={cfg} onSaved={reload} />
      )}
    </LoadView>
  );
}

function MailLimitsTab() {
  const [load, reload] = useLoad(() => api.mailLimits(), []);
  return (
    <LoadView load={load} reload={reload}>
      {(limits) => <LimitsForm initial={limits} onSaved={reload} />}
    </LoadView>
  );
}

const TABS = [
  "notifications",
  "storage",
  "monitoring",
  "mail",
  "security",
  "blacklist",
  "restoreTest",
  "agent",
] as const;
type Tab = (typeof TABS)[number];

export function SettingsPage() {
  const { t } = useTranslation();
  const { canManageSettings } = useAuth();
  const [settings, reload] = useLoad<NotificationSettings>(
    () => api.notificationSettings(),
    [],
  );
  const [current, setCurrent] = useState<NotificationSettings | null>(null);
  const [refresh, setRefresh] = useState(0);
  const [params, setParams] = useSearchParams();
  if (!canManageSettings) {
    return (
      <p role="alert" className="alert error">
        {t("settings.forbidden")}
      </p>
    );
  }
  const tab: Tab = TABS.includes(params.get("tab") as Tab)
    ? (params.get("tab") as Tab)
    : "notifications";
  return (
    <>
      <h1>{t("settings.title")}</h1>
      <div className="tabs" role="tablist" aria-label={t("settings.title")}>
        {TABS.map((name) => (
          <button
            key={name}
            type="button"
            role="tab"
            aria-selected={tab === name}
            className={tab === name ? "" : "secondary"}
            onClick={() =>
              setParams(name === "notifications" ? {} : { tab: name })
            }
          >
            {t(`settings.tabs.${name}`)}
          </button>
        ))}
      </div>
      {tab === "notifications" && (
        <>
          <LoadView load={settings} reload={reload}>
            {(s) => (
              <NotificationsForm
                initial={current ?? s}
                onSaved={(saved) => {
                  setCurrent(saved);
                  setRefresh((n) => n + 1);
                }}
              />
            )}
          </LoadView>
          <TelegramSection events={EVENTS} />
          <Outbox refresh={refresh} />
        </>
      )}
      {tab === "storage" && <StorageSettingsSection />}
      {tab === "monitoring" && <MonitoringSettingsTab />}
      {tab === "mail" && <MailLimitsTab />}
      {tab === "security" && <PolicyCard />}
      {tab === "blacklist" && <BlacklistSettingsTab />}
      {tab === "restoreTest" && <RestoreTestSettingsTab />}
      {tab === "agent" && <AgentTab />}
    </>
  );
}
