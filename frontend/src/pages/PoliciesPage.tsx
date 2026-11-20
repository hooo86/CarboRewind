import { useEffect, useState } from "react";
import { useLocation } from "react-router-dom";
import type { FormEvent } from "react";
import { useTranslation } from "react-i18next";

import { api } from "../api/endpoints";
import type {
  AccountRow,
  BackupType,
  Domain,
  Page,
  Policy,
  PolicyInput,
  PolicyRun,
  RetentionRule,
  TargetType,
} from "../api/types";
import { useAuth } from "../auth/AuthContext";
import {
  LoadView,
  StatusBadge,
  errorMessage,
  useLoad,
} from "../components/common";
import { IconButton } from "../components/IconButton";
import { formatBytes, formatDate, formatRelative } from "../utils/format";

type Preset = "daily" | "weekly" | "custom";
const DAYS = [1, 2, 3, 4, 5, 6, 0];

function browserZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

/** "0 1 * * *" -> daily 01:00; "30 2 * * 0" -> weekly Sunday 02:30; else custom. */
export function readSchedule(cron: string): {
  preset: Preset;
  time: string;
  day: number;
} {
  const m = /^(\d{1,2}) (\d{1,2}) \* \* (\*|[0-7])$/.exec(cron.trim());
  if (!m) return { preset: "custom", time: "01:00", day: 0 };
  const time = `${m[2]?.padStart(2, "0")}:${m[1]?.padStart(2, "0")}`;
  if (m[3] === "*") return { preset: "daily", time, day: 0 };
  return { preset: "weekly", time, day: Number(m[3]) % 7 };
}

export function buildSchedule(
  preset: Preset,
  time: string,
  day: number,
  custom: string,
): string {
  if (preset === "custom") return custom.trim();
  const [h = "1", m = "0"] = time.split(":");
  return `${Number(m)} ${Number(h)} * * ${preset === "daily" ? "*" : day}`;
}

type FullMode = "none" | "weekly" | "monthly" | "custom";

/** "0 2 * * 0" -> weekly Sunday 02:00; "0 2 1 * *" -> monthly on the 1st; else custom. */
export function readFull(cron: string | null | undefined): {
  mode: FullMode;
  time: string;
  day: number;
  dom: number;
} {
  if (!cron) return { mode: "none", time: "02:00", day: 0, dom: 1 };
  const weekly = /^(\d{1,2}) (\d{1,2}) \* \* ([0-7])$/.exec(cron.trim());
  const monthly = /^(\d{1,2}) (\d{1,2}) (\d{1,2}) \* \*$/.exec(cron.trim());
  const m = weekly ?? monthly;
  if (!m) return { mode: "custom", time: "02:00", day: 0, dom: 1 };
  const time = `${m[2]?.padStart(2, "0")}:${m[1]?.padStart(2, "0")}`;
  return weekly
    ? { mode: "weekly", time, day: Number(m[3]) % 7, dom: 1 }
    : { mode: "monthly", time, day: 0, dom: Number(m[3]) };
}

export function buildFull(
  mode: FullMode,
  time: string,
  day: number,
  dom: number,
  custom: string,
): string {
  if (mode === "none") return "";
  if (mode === "custom") return custom.trim();
  const [h = "2", m = "0"] = time.split(":");
  return mode === "weekly"
    ? `${Number(m)} ${Number(h)} * * ${day}`
    : `${Number(m)} ${Number(h)} ${dom} * *`;
}

function fullLabel(
  cron: string,
  t: (k: string, o?: Record<string, unknown>) => string,
): string {
  const f = readFull(cron);
  if (f.mode === "weekly")
    return t("policies.fullWeekly", { day: t(`days.${f.day}`), time: f.time });
  if (f.mode === "monthly")
    return t("policies.fullMonthly", { dom: f.dom, time: f.time });
  return t("policies.fullCron", { cron });
}

function scheduleLabel(
  p: Policy,
  t: (k: string, o?: Record<string, unknown>) => string,
): string {
  const s = readSchedule(p.schedule);
  if (s.preset === "daily") return t("policies.everyDay", { time: s.time });
  if (s.preset === "weekly")
    return t("policies.everyWeek", { day: t(`days.${s.day}`), time: s.time });
  return p.schedule;
}

function retentionLabel(
  r: RetentionRule,
  t: (k: string, o?: Record<string, unknown>) => string,
): string {
  const parts = (["last", "daily", "weekly", "monthly"] as const)
    .filter((k) => r[k] > 0)
    .map((k) => t(`policies.keep.${k}`, { count: r[k] }));
  return parts.length ? parts.join(", ") : t("policies.keepAll");
}

/** ``last_run_at`` starts at creation time: no slot has been processed yet. */
function neverRan(p: Policy): boolean {
  return (
    !p.last_run_at ||
    new Date(p.last_run_at).getTime() - new Date(p.created_at).getTime() <
      60_000
  );
}

const EMPTY: PolicyInput = {
  name: "",
  target_type: "domain",
  target_id: null,
  backup_type: "incremental",
  schedule: "0 1 * * *",
  timezone: browserZone(),
  retention: { last: 0, daily: 14, weekly: 8, monthly: 12 },
  enabled: true,
};

export type Preselection = { accountIds: string[]; emails: string[] };

function PolicyForm({
  initial,
  domains,
  preselection,
  onSaved,
  onCancel,
}: {
  initial: Policy | null;
  domains: Domain[];
  preselection?: Preselection | null;
  onSaved: () => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  const { canManagePolicies } = useAuth();
  const start: PolicyInput = initial
    ? {
        name: initial.name,
        target_type: initial.target_type,
        target_id: initial.target_id,
        account_ids: initial.account_ids,
        backup_type: initial.backup_type,
        schedule: initial.schedule,
        timezone: initial.timezone,
        retention: initial.retention,
        enabled: initial.enabled,
        full_schedule: initial.full_schedule ?? null,
      }
    : preselection
      ? {
          ...EMPTY,
          target_type: "accounts",
          target_id: null,
          account_ids: preselection.accountIds,
        }
      : {
          ...EMPTY,
          target_id: domains.find((d) => canManagePolicies(d.id))?.id ?? null,
        };
  const [form, setForm] = useState<PolicyInput>(start);
  const parsed = readSchedule(start.schedule);
  const [preset, setPreset] = useState<Preset>(parsed.preset);
  const [time, setTime] = useState(parsed.time);
  const [day, setDay] = useState(parsed.day);
  const [custom, setCustom] = useState(start.schedule);
  const full = readFull(start.full_schedule);
  const [fullMode, setFullMode] = useState<FullMode>(full.mode);
  const [fullTime, setFullTime] = useState(full.time);
  const [fullDay, setFullDay] = useState(full.day);
  const [fullDom, setFullDom] = useState(full.dom);
  const [fullCustom, setFullCustom] = useState(start.full_schedule ?? "");
  const [accountDomain, setAccountDomain] = useState(domains[0]?.id ?? "");
  const [accountQuery, setAccountQuery] = useState(
    initial?.target_type === "account" ? (initial.target_name ?? "") : "",
  );
  const [matches, setMatches] = useState<AccountRow[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (form.target_type !== "account" || accountQuery.trim().length < 2) {
      setMatches([]);
      return;
    }
    let cancelled = false;
    const timer = window.setTimeout(() => {
      api
        .accounts(accountDomain, accountQuery.trim())
        .then((p) => !cancelled && setMatches(p.items.slice(0, 8)))
        .catch(() => !cancelled && setMatches([]));
    }, 250);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [form.target_type, accountQuery, accountDomain]);

  const set = <K extends keyof PolicyInput>(key: K, value: PolicyInput[K]) =>
    setForm((f) => ({ ...f, [key]: value }));
  const setKeep = (key: keyof RetentionRule, value: string) =>
    set("retention", {
      ...form.retention,
      [key]: Math.max(0, Number(value) || 0),
    });

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    const body: PolicyInput = {
      ...form,
      target_id:
        form.target_type === "global" || form.target_type === "accounts"
          ? null
          : form.target_id,
      schedule: buildSchedule(preset, time, day, custom),
      full_schedule: buildFull(
        fullMode,
        fullTime,
        fullDay,
        fullDom,
        fullCustom,
      ),
    };
    if (form.target_type !== "accounts") delete body.account_ids;
    try {
      if (initial) {
        const {
          target_type: _tt,
          target_id: _ti,
          account_ids: _ai,
          ...patch
        } = body;
        void _tt;
        void _ti;
        void _ai;
        await api.updatePolicy(initial.id, patch);
      } else {
        await api.createPolicy(body);
      }
      onSaved();
    } catch (err) {
      setError(t("error", { message: errorMessage(err) }));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="card policy-form" onSubmit={(e) => void submit(e)}>
      <h2>{initial ? t("policies.edit") : t("policies.new")}</h2>
      <label>
        {t("policies.name")}
        <input
          required
          maxLength={255}
          value={form.name}
          onChange={(e) => set("name", e.target.value)}
        />
      </label>

      <fieldset disabled={initial !== null}>
        <legend>{t("policies.target")}</legend>
        <div className="row">
          <label>
            {t("policies.targetType")}
            <select
              value={form.target_type}
              onChange={(e) => {
                const type = e.target.value as TargetType;
                set("target_type", type);
                set(
                  "target_id",
                  type === "domain" ? (domains[0]?.id ?? null) : null,
                );
              }}
            >
              {canManagePolicies(null) && (
                <option value="global">{t("policies.targets.global")}</option>
              )}
              <option value="domain">{t("policies.targets.domain")}</option>
              <option value="account">{t("policies.targets.account")}</option>
              {form.target_type === "accounts" && (
                <option value="accounts">
                  {t("policies.targets.accounts")}
                </option>
              )}
            </select>
          </label>
          {form.target_type === "accounts" && (
            <p className="selection-summary">
              {t("policies.selectionCount", {
                count: form.account_ids?.length ?? 0,
              })}
              {preselection && (
                <span className="muted">
                  {" "}
                  ({preselection.emails.slice(0, 5).join(", ")}
                  {preselection.emails.length > 5 ? ", …" : ""})
                </span>
              )}
            </p>
          )}
          {form.target_type === "domain" && (
            <label>
              {t("accounts.domain")}
              <select
                value={form.target_id ?? ""}
                onChange={(e) => set("target_id", e.target.value)}
              >
                {domains
                  .filter((d) => canManagePolicies(d.id))
                  .map((d) => (
                    <option key={d.id} value={d.id}>
                      {d.name}
                    </option>
                  ))}
              </select>
            </label>
          )}
          {form.target_type === "account" && (
            <>
              <label>
                {t("accounts.domain")}
                <select
                  value={accountDomain}
                  onChange={(e) => setAccountDomain(e.target.value)}
                >
                  {domains.map((d) => (
                    <option key={d.id} value={d.id}>
                      {d.name}
                    </option>
                  ))}
                </select>
              </label>
              <label className="grow">
                {t("policies.account")}
                <input
                  type="search"
                  value={accountQuery}
                  placeholder={t("accounts.searchPlaceholder")}
                  onChange={(e) => {
                    setAccountQuery(e.target.value);
                    set("target_id", null);
                  }}
                />
              </label>
            </>
          )}
        </div>
        {form.target_type === "account" &&
          !form.target_id &&
          matches.length > 0 && (
            <ul className="suggest">
              {matches.map((a) => (
                <li key={a.id}>
                  <button
                    type="button"
                    className="link"
                    onClick={() => {
                      set("target_id", a.id);
                      setAccountQuery(a.email);
                      setMatches([]);
                    }}
                  >
                    {a.email}
                  </button>
                </li>
              ))}
            </ul>
          )}
      </fieldset>

      <fieldset>
        <legend>{t("policies.when")}</legend>
        <div className="row">
          <label>
            {t("policies.frequency")}
            <select
              value={preset}
              onChange={(e) => setPreset(e.target.value as Preset)}
            >
              <option value="daily">{t("policies.presets.daily")}</option>
              <option value="weekly">{t("policies.presets.weekly")}</option>
              <option value="custom">{t("policies.presets.custom")}</option>
            </select>
          </label>
          {preset === "weekly" && (
            <label>
              {t("policies.day")}
              <select
                value={day}
                onChange={(e) => setDay(Number(e.target.value))}
              >
                {DAYS.map((d) => (
                  <option key={d} value={d}>
                    {t(`days.${d}`)}
                  </option>
                ))}
              </select>
            </label>
          )}
          {preset !== "custom" ? (
            <label>
              {t("policies.time")}
              <input
                type="time"
                required
                value={time}
                onChange={(e) => setTime(e.target.value)}
              />
            </label>
          ) : (
            <label className="grow">
              {t("policies.cron")}
              <input
                required
                value={custom}
                placeholder="0 1 * * *"
                onChange={(e) => setCustom(e.target.value)}
              />
              <span className="hint">{t("policies.cronHint")}</span>
            </label>
          )}
          <label>
            {t("policies.timezone")}
            <input
              required
              value={form.timezone}
              onChange={(e) => set("timezone", e.target.value)}
            />
          </label>
          <label>
            {t("policies.backupType")}
            <select
              value={form.backup_type}
              onChange={(e) => set("backup_type", e.target.value as BackupType)}
            >
              <option value="incremental">{t("backupType.incremental")}</option>
              <option value="full">{t("backupType.full")}</option>
            </select>
          </label>
        </div>
      </fieldset>

      <fieldset>
        <legend>{t("policies.full")}</legend>
        <p className="hint">{t("policies.fullHint")}</p>
        <div className="row">
          <label>
            {t("policies.fullMode")}
            <select
              value={fullMode}
              onChange={(e) => setFullMode(e.target.value as FullMode)}
            >
              <option value="none">{t("policies.fullModes.none")}</option>
              <option value="weekly">{t("policies.fullModes.weekly")}</option>
              <option value="monthly">{t("policies.fullModes.monthly")}</option>
              <option value="custom">{t("policies.fullModes.custom")}</option>
            </select>
          </label>
          {fullMode === "weekly" && (
            <label>
              {t("policies.day")}
              <select
                value={fullDay}
                onChange={(e) => setFullDay(Number(e.target.value))}
              >
                {DAYS.map((d) => (
                  <option key={d} value={d}>
                    {t(`days.${d}`)}
                  </option>
                ))}
              </select>
            </label>
          )}
          {fullMode === "monthly" && (
            <label>
              {t("policies.dayOfMonth")}
              <input
                type="number"
                min={1}
                max={28}
                value={fullDom}
                onChange={(e) =>
                  setFullDom(
                    Math.min(28, Math.max(1, Number(e.target.value) || 1)),
                  )
                }
              />
            </label>
          )}
          {(fullMode === "weekly" || fullMode === "monthly") && (
            <label>
              {t("policies.time")}
              <input
                type="time"
                required
                value={fullTime}
                onChange={(e) => setFullTime(e.target.value)}
              />
            </label>
          )}
          {fullMode === "custom" && (
            <label className="grow">
              {t("policies.cron")}
              <input
                required
                value={fullCustom}
                placeholder="0 2 * * 0"
                onChange={(e) => setFullCustom(e.target.value)}
              />
              <span className="hint">{t("policies.cronHint")}</span>
            </label>
          )}
        </div>
      </fieldset>

      <fieldset>
        <legend>{t("policies.retention")}</legend>
        <p className="hint">{t("policies.retentionHint")}</p>
        <div className="row">
          {(["last", "daily", "weekly", "monthly"] as const).map((k) => (
            <label key={k}>
              {t(`policies.retentionFields.${k}`)}
              <input
                type="number"
                min={0}
                max={10000}
                value={form.retention[k]}
                onChange={(e) => setKeep(k, e.target.value)}
              />
            </label>
          ))}
        </div>
      </fieldset>

      <label className="check">
        <input
          type="checkbox"
          checked={form.enabled}
          onChange={(e) => set("enabled", e.target.checked)}
        />
        {t("policies.enabled")}
      </label>
      {error && (
        <p role="alert" className="alert error">
          {error}
        </p>
      )}
      <div className="actions">
        <button
          type="submit"
          disabled={
            busy ||
            (form.target_type === "accounts"
              ? !form.account_ids?.length
              : form.target_type !== "global" && !form.target_id)
          }
        >
          {t("policies.save")}
        </button>
        <button type="button" className="secondary" onClick={onCancel}>
          {t("policies.cancel")}
        </button>
      </div>
    </form>
  );
}

function runDuration(run: PolicyRun): string {
  if (!run.finished_at) return "—";
  const seconds = Math.max(
    0,
    Math.round(
      (new Date(run.finished_at).getTime() -
        new Date(run.started_at).getTime()) /
        1000,
    ),
  );
  if (seconds < 60) return `${seconds} s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} min`;
  return `${Math.floor(minutes / 60)} h ${minutes % 60} min`;
}

/** Execution history of one policy, newest first. */
function PolicyHistory({
  policy,
  onClose,
}: {
  policy: Policy;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const [offset, setOffset] = useState(0);
  const [runs, reload] = useLoad<Page<PolicyRun>>(
    () => api.policyRuns(policy.id, offset),
    [policy.id, offset],
    10000,
  );
  return (
    <section
      className="card"
      aria-label={t("policies.historyOf", { name: policy.name })}
    >
      <div className="page-head">
        <h2>{t("policies.historyOf", { name: policy.name })}</h2>
        <button type="button" className="secondary small" onClick={onClose}>
          {t("policies.close")}
        </button>
      </div>
      <p className="muted small">
        {t("policies.totals", {
          accounts: policy.account_count ?? 0,
          protected: formatBytes(policy.protected_bytes ?? 0),
          stored: formatBytes(policy.stored_bytes ?? 0),
          snapshots: policy.snapshot_count ?? 0,
        })}
      </p>
      <LoadView load={runs} reload={reload}>
        {(page) =>
          page.items.length === 0 ? (
            <p className="muted">{t("policies.noRun")}</p>
          ) : (
            <>
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>{t("policies.runDate")}</th>
                      <th>{t("policies.trigger")}</th>
                      <th>{t("policies.backupType")}</th>
                      <th>{t("policies.result")}</th>
                      <th className="num">{t("policies.runAccounts")}</th>
                      <th>{t("jobs.duration")}</th>
                      <th className="num">{t("policies.read")}</th>
                      <th className="num">{t("policies.written")}</th>
                      <th className="num">{t("policies.size")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {page.items.map((run) => (
                      <tr key={`${run.started_at}-${run.backup_type}`}>
                        <td>{formatDate(run.started_at)}</td>
                        <td>{t(`policies.triggers.${run.trigger}`)}</td>
                        <td>{t(`backupType.${run.backup_type}`)}</td>
                        <td>
                          <StatusBadge status={run.status} />
                        </td>
                        <td className="num">
                          {t("policies.runCount", {
                            ok: run.succeeded,
                            total: run.accounts,
                          })}
                          {run.failed > 0 && (
                            <>
                              <br />
                              <span className="bad-text small">
                                {t("policies.runFailed", { count: run.failed })}
                              </span>
                            </>
                          )}
                          {run.pending > 0 && (
                            <>
                              <br />
                              <span className="muted small">
                                {t("policies.runPending", {
                                  count: run.pending,
                                })}
                              </span>
                            </>
                          )}
                        </td>
                        <td>{runDuration(run)}</td>
                        <td className="num">{formatBytes(run.read_bytes)}</td>
                        <td className="num">{formatBytes(run.new_bytes)}</td>
                        <td className="num">{formatBytes(run.size_bytes)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="pager">
                <span className="muted">
                  {t("jobs.range", {
                    from: page.offset + 1,
                    to: page.offset + page.items.length,
                    total: page.total,
                  })}
                </span>
                <button
                  type="button"
                  className="secondary small"
                  disabled={offset === 0}
                  onClick={() => setOffset(Math.max(0, offset - page.limit))}
                >
                  {t("jobs.previous")}
                </button>
                <button
                  type="button"
                  className="secondary small"
                  disabled={offset + page.items.length >= page.total}
                  onClick={() => setOffset(offset + page.limit)}
                >
                  {t("jobs.next")}
                </button>
              </div>
            </>
          )
        }
      </LoadView>
    </section>
  );
}

export function PoliciesPage() {
  const { t } = useTranslation();
  const { canManagePolicies } = useAuth();
  const [policies, reload] = useLoad<Policy[]>(() => api.policies(), []);
  const [domains] = useLoad<Page<Domain>>(() => api.domains(), []);
  const location = useLocation();
  const incoming = (location.state as Preselection | null) ?? null;
  const [preselection, setPreselection] = useState<Preselection | null>(
    incoming?.accountIds?.length ? incoming : null,
  );
  const [editing, setEditing] = useState<Policy | "new" | null>(
    incoming?.accountIds?.length ? "new" : null,
  );
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);
  const [history, setHistory] = useState<Policy | null>(null);
  const domainList = domains.state === "ready" ? domains.data.items : [];
  const canCreate =
    domains.state === "ready" &&
    (canManagePolicies(null) ||
      domainList.some((d) => canManagePolicies(d.id)));

  const owns = (p: Policy, ownDomains: Domain[]) =>
    p.target_type === "global"
      ? canManagePolicies(null)
      : p.target_type === "accounts"
        ? canManagePolicies(null) ||
          ownDomains.some((d) => canManagePolicies(d.id))
        : canManagePolicies(null) ||
          ownDomains.some(
            (d) =>
              canManagePolicies(d.id) &&
              (p.target_id === d.id || p.target_name?.endsWith(`@${d.name}`)),
          );

  async function remove(p: Policy) {
    if (!window.confirm(t("policies.confirmDelete", { name: p.name }))) return;
    try {
      await api.deletePolicy(p.id);
      reload();
    } catch (err) {
      setError(t("error", { message: errorMessage(err) }));
    }
  }

  async function runNow(p: Policy) {
    if (!window.confirm(t("policies.confirmRun", { name: p.name }))) return;
    setError(null);
    setInfo(null);
    try {
      const r = await api.runPolicy(p.id);
      setInfo(
        t("policies.runQueued", {
          name: p.name,
          count: r.queued,
          busy: r.skipped_busy,
        }),
      );
    } catch (err) {
      setError(t("error", { message: errorMessage(err) }));
    }
  }

  async function toggle(p: Policy) {
    try {
      await api.updatePolicy(p.id, { enabled: !p.enabled });
      reload();
    } catch (err) {
      setError(t("error", { message: errorMessage(err) }));
    }
  }

  return (
    <>
      <div className="page-head">
        <h1>{t("policies.title")}</h1>
        {canCreate && editing === null && (
          <button type="button" onClick={() => setEditing("new")}>
            {t("policies.new")}
          </button>
        )}
      </div>
      <p className="muted">{t("policies.intro")}</p>
      {error && (
        <p role="alert" className="alert error">
          {error}
        </p>
      )}
      {info && (
        <p role="status" className="alert ok">
          {info}
        </p>
      )}
      {editing !== null && (
        <PolicyForm
          initial={editing === "new" ? null : editing}
          preselection={editing === "new" ? preselection : null}
          domains={domainList}
          onSaved={() => {
            setEditing(null);
            setPreselection(null);
            reload();
          }}
          onCancel={() => {
            setEditing(null);
            setPreselection(null);
          }}
        />
      )}
      <LoadView load={policies} reload={reload}>
        {(list) =>
          list.length === 0 ? (
            <p className="alert warn">{t("policies.none")}</p>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>{t("policies.name")}</th>
                    <th>{t("policies.target")}</th>
                    <th>{t("policies.when")}</th>
                    <th>{t("policies.retention")}</th>
                    <th className="num">{t("policies.volume")}</th>
                    <th>{t("policies.next")}</th>
                    <th>{t("policies.last")}</th>
                    <th>{t("policies.state")}</th>
                    <th>{t("accounts.actions")}</th>
                  </tr>
                </thead>
                <tbody>
                  {list.map((p) => (
                    <tr key={p.id}>
                      <td>{p.name}</td>
                      <td className="wrap">
                        {p.target_type === "global"
                          ? t("policies.targets.global")
                          : p.target_type === "accounts"
                            ? t("policies.selectionCount", {
                                count: p.account_ids.length,
                              })
                            : (p.target_name ?? "—")}
                      </td>
                      <td>
                        {scheduleLabel(p, t)}
                        <br />
                        <span className="muted">
                          {t(`backupType.${p.backup_type}`)} · {p.timezone}
                        </span>
                        {p.full_schedule && (
                          <>
                            <br />
                            <span className="small">
                              + {fullLabel(p.full_schedule, t)}
                            </span>
                          </>
                        )}
                      </td>
                      <td className="wrap">{retentionLabel(p.retention, t)}</td>
                      <td
                        className="num"
                        title={t("policies.volumeHelp", {
                          stored: formatBytes(p.stored_bytes ?? 0),
                          snapshots: p.snapshot_count ?? 0,
                        })}
                      >
                        {formatBytes(p.protected_bytes ?? 0)}
                        <br />
                        <span className="muted small">
                          {t("policies.accountCount", {
                            count: p.account_count ?? 0,
                          })}
                        </span>
                      </td>
                      <td title={formatDate(p.next_run_at)}>
                        {p.next_run_at ? formatRelative(p.next_run_at) : "—"}
                        {p.next_full_run_at && (
                          <>
                            <br />
                            <span
                              className="muted small"
                              title={formatDate(p.next_full_run_at)}
                            >
                              {t("policies.nextFull", {
                                when: formatRelative(p.next_full_run_at),
                              })}
                            </span>
                          </>
                        )}
                      </td>
                      <td>
                        {p.last_run ? (
                          <>
                            <StatusBadge status={p.last_run.status} />{" "}
                            <span className="small">
                              {t("policies.runCount", {
                                ok: p.last_run.succeeded,
                                total: p.last_run.accounts,
                              })}
                            </span>
                            <br />
                            <span className="muted small">
                              {formatDate(p.last_run.started_at)}
                            </span>
                          </>
                        ) : neverRan(p) ? (
                          <span className="muted">{t("accounts.never")}</span>
                        ) : (
                          formatDate(p.last_run_at)
                        )}
                      </td>
                      <td>
                        <StatusBadge
                          status={p.enabled ? "enabled" : "disabled"}
                        />
                      </td>
                      <td>
                        <span className="actions icon-actions">
                          <IconButton
                            icon="history"
                            label={t("policies.history")}
                            title={t("policies.historyOf", { name: p.name })}
                            pressed={history?.id === p.id}
                            onClick={() =>
                              setHistory(history?.id === p.id ? null : p)
                            }
                          />
                          {owns(p, domainList) && (
                            <>
                              <IconButton
                                icon="play"
                                tone="primary"
                                label={t("policies.run")}
                                title={t("policies.runTitle", { name: p.name })}
                                onClick={() => void runNow(p)}
                              />
                              <IconButton
                                icon="edit"
                                label={t("policies.editShort")}
                                title={t("policies.editTitle", {
                                  name: p.name,
                                })}
                                onClick={() => setEditing(p)}
                              />
                              <IconButton
                                icon={p.enabled ? "pause" : "resume"}
                                label={
                                  p.enabled
                                    ? t("policies.disable")
                                    : t("policies.enable")
                                }
                                title={t(
                                  p.enabled
                                    ? "policies.disableTitle"
                                    : "policies.enableTitle",
                                  { name: p.name },
                                )}
                                onClick={() => void toggle(p)}
                              />
                              <IconButton
                                icon="trash"
                                tone="danger"
                                label={t("policies.delete")}
                                title={t("policies.deleteTitle", {
                                  name: p.name,
                                })}
                                onClick={() => void remove(p)}
                              />
                            </>
                          )}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )
        }
      </LoadView>
      {history && (
        <PolicyHistory
          key={history.id}
          policy={
            (policies.state === "ready" &&
              policies.data.find((p) => p.id === history.id)) ||
            history
          }
          onClose={() => setHistory(null)}
        />
      )}
    </>
  );
}
