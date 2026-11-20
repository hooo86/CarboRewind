import { Fragment, useEffect, useState } from "react";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";

import { api } from "../api/endpoints";
import type {
  BackupType,
  Dashboard,
  Domain,
  Job,
  JobStatus,
  LoginSecurity,
  MailOverview,
  MailPeriod,
  Page,
  Policy,
  Widget,
  WidgetType,
} from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { JobTable } from "../components/JobTable";
import {
  BlockedIpsPanel,
  SpamSourcesPanel,
} from "../components/SecurityPanels";
import {
  LoadView,
  StatusBadge,
  errorMessage,
  useLoad,
} from "../components/common";
import { formatBytes, formatDate, formatRelative } from "../utils/format";

function Tile({
  label,
  value,
  tone,
}: {
  label: string;
  value: string | number;
  tone?: string;
}) {
  return (
    <div className={`tile ${tone ?? ""}`}>
      <span className="tile-value">{value}</span>
      <span className="tile-label">{label}</span>
    </div>
  );
}

function Gauge({
  label,
  used,
  total,
  detail,
  error,
}: {
  label: string;
  used: number;
  total: number;
  detail?: string;
  error?: boolean;
}) {
  const { t } = useTranslation();
  const pct = total > 0 ? Math.min(100, Math.round((used / total) * 100)) : 0;
  const tone = error ? "bad" : pct >= 90 ? "bad" : pct >= 75 ? "warn" : "ok";
  return (
    <div className="gauge">
      <div className="gauge-head">
        <strong>{label}</strong>
        <span className={`${tone}-text`}>
          {error
            ? t("dashboard.storageUnreachable")
            : t("dashboard.usedOf", {
                used: formatBytes(used),
                total: formatBytes(total),
                pct,
              })}
        </span>
      </div>
      <div
        className="gauge-bar"
        role="meter"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={pct}
      >
        <span className={`gauge-fill ${tone}`} style={{ width: `${pct}%` }} />
      </div>
      {detail && <p className="muted gauge-detail">{detail}</p>}
    </div>
  );
}

function StorageOverview({ d }: { d: Dashboard }) {
  const { t, i18n } = useTranslation();
  const mail = d.mail_used_bytes ?? 0;
  const pctOf = (total?: number | null) =>
    total
      ? (Math.round((mail / total) * 1000) / 10).toLocaleString(i18n.language)
      : null;
  const reports = d.storage ?? [];
  return (
    <div className="stack">
      <div className="tiles">
        <div className="tile">
          <span className="tile-value">{formatBytes(mail)}</span>
          <span className="tile-label">{t("dashboard.mailVolume")}</span>
        </div>
        {d.mail_quota_bytes ? (
          <div className="tile">
            <span className="tile-value">
              {formatBytes(d.mail_quota_bytes)}
            </span>
            <span className="tile-label">{t("dashboard.quotaAllocated")}</span>
          </div>
        ) : null}
        {d.archived_messages ? (
          <div className="tile">
            <span className="tile-value">
              {formatBytes(d.archived_bytes ?? 0)}
            </span>
            <span className="tile-label">
              {t("dashboard.archived", {
                count: d.archived_messages,
                freed: formatBytes(d.archived_removed_bytes ?? 0),
              })}
            </span>
          </div>
        ) : null}
      </div>
      {(d.mailboxes_nearly_full ?? 0) > 0 && (
        <p role="alert" className="alert warn">
          {t("dashboard.nearlyFull", {
            count: d.mailboxes_nearly_full,
            pct: d.mailbox_alert_percent,
          })}{" "}
          <Link
            to={
              d.mailbox_alert_percent
                ? `/accounts?f=usage_pct:ge:${d.mailbox_alert_percent}`
                : "/monitoring"
            }
          >
            {t("dashboard.seeThem")}
          </Link>
        </p>
      )}
      {reports.map((st) => {
        const server = st.mail_store;
        const serverPct = pctOf(server?.total_bytes);
        const nasPct = pctOf(st.total_bytes);
        return (
          <div key={st.agent + st.path} className="split">
            {server?.total_bytes ? (
              <Gauge
                label={t("dashboard.serverDisk", { agent: st.agent })}
                used={server.total_bytes - (server.free_bytes ?? 0)}
                total={server.total_bytes}
                error={!!server.error}
                detail={
                  serverPct !== null
                    ? t("dashboard.mailShareServer", { pct: serverPct })
                    : undefined
                }
              />
            ) : null}
            {st.total_bytes ? (
              <Gauge
                label={
                  st.kind === "network"
                    ? t("dashboard.backupNas", { where: st.source ?? st.path })
                    : t("dashboard.backupLocal")
                }
                used={st.total_bytes - (st.free_bytes ?? 0)}
                total={st.total_bytes}
                error={!!st.error}
                detail={
                  nasPct !== null
                    ? t("dashboard.mailShareNas", { pct: nasPct })
                    : undefined
                }
              />
            ) : (
              st.error && (
                <p className="alert warn">
                  {t("dashboard.storageUnreachable")}
                </p>
              )
            )}
            {st.replica?.total_bytes ? (
              <Gauge
                label={t("dashboard.copy2", {
                  where: st.replica.source ?? st.replica.path,
                })}
                used={st.replica.total_bytes - (st.replica.free_bytes ?? 0)}
                total={st.replica.total_bytes}
                error={!!st.replica.error}
              />
            ) : null}
          </div>
        );
      })}
    </div>
  );
}

function ManualBackup({ onQueued }: { onQueued: () => void }) {
  const { t } = useTranslation();
  const { canRunBackup } = useAuth();
  const [domains] = useLoad<Page<Domain>>(() => api.domains(), []);
  const [type, setType] = useState<BackupType>("incremental");
  const [domain, setDomain] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<{ ok: boolean; text: string } | null>(
    null,
  );
  const allowed =
    domains.state === "ready"
      ? domains.data.items.filter((d) => canRunBackup(d.id))
      : [];
  if (domains.state !== "ready") return null;
  if (allowed.length === 0)
    return <p className="muted">{t("widgets.noBackupRight")}</p>;

  async function run() {
    const scope = domain
      ? (allowed.find((d) => d.id === domain)?.name ?? "")
      : t("manual.allDomains");
    if (
      !window.confirm(
        t("manual.confirm", { scope, type: t(`backupType.${type}`) }),
      )
    )
      return;
    setBusy(true);
    setNotice(null);
    try {
      const r = await api.backupAll(type, domain || undefined);
      setNotice({
        ok: true,
        text: t("manual.queued", { count: r.queued, busy: r.skipped_busy }),
      });
      onQueued();
    } catch (err) {
      setNotice({
        ok: false,
        text: t("error", { message: errorMessage(err) }),
      });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="stack">
      <p className="muted">{t("manual.help")}</p>
      <div className="row">
        <label>
          {t("manual.scope")}
          <select value={domain} onChange={(e) => setDomain(e.target.value)}>
            <option value="">{t("manual.allDomains")}</option>
            {allowed.map((d) => (
              <option key={d.id} value={d.id}>
                {d.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          {t("manual.type")}
          <select
            value={type}
            onChange={(e) => setType(e.target.value as BackupType)}
          >
            <option value="incremental">{t("backupType.incremental")}</option>
            <option value="full">{t("backupType.full")}</option>
          </select>
        </label>
        <button
          type="button"
          className="align-end"
          disabled={busy}
          onClick={() => void run()}
        >
          {t("manual.run")}
        </button>
      </div>
      {notice && (
        <p
          role={notice.ok ? "status" : "alert"}
          className={`alert ${notice.ok ? "ok" : "error"}`}
        >
          {notice.text}
        </p>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ widgets */

export const KPIS = [
  "accounts",
  "protected",
  "never",
  "partial",
  "queued",
  "running",
  "failed",
  "stored",
  "agents",
  "mail_volume",
  "archived",
] as const;
type Kpi = (typeof KPIS)[number];

const ADMIN_WIDGETS = new Set<WidgetType>([
  "mail_top",
  "spam_sources",
  "blocked_ips",
]);
export const WIDGET_TYPES: WidgetType[] = [
  "kpis",
  "storage",
  "manual_backup",
  "jobs",
  "policies",
  "blocked_ips",
  "spam_sources",
  "mail_top",
];
const PERIODS: MailPeriod[] = ["1h", "24h", "7d", "30d"];
const COUNTS = [5, 10, 20, 50];
const JOB_STATUSES = ["", "failed", "running", "queued", "succeeded"];
const JOB_KINDS = [
  "",
  "backup",
  "restore",
  "archive",
  "migrate",
  "verify",
  "purge",
];

/** Default options of a widget type (also the editor's starting values). */
export function defaultOptions(type: WidgetType): Record<string, unknown> {
  switch (type) {
    case "kpis":
      return { items: [...KPIS] };
    case "jobs":
      return { count: 10, status: "", kind: "" };
    case "mail_top":
    case "spam_sources":
      return { period: "24h", count: 10 };
    case "blocked_ips":
      return { period: "24h", count: 10, only_blocked: false };
    default:
      return {};
  }
}

export function defaultLayout(admin: boolean): Widget[] {
  const w = (
    id: string,
    type: WidgetType,
    size: Widget["size"] = "full",
    options: Record<string, unknown> = defaultOptions(type),
  ): Widget => ({ id, type, size, options });
  return [
    w("kpis", "kpis"),
    w("storage", "storage"),
    ...(admin
      ? [w("blocked", "blocked_ips", "half"), w("spam", "spam_sources", "half")]
      : []),
    w("manual", "manual_backup"),
    w("jobs", "jobs"),
  ];
}

const opt = <T,>(w: Widget, key: string, fallback: T): T =>
  (w.options[key] as T | undefined) ?? fallback;

function KpiTiles({ d, items }: { d: Dashboard; items: Kpi[] }) {
  const { t } = useTranslation();
  const tiles: Record<Kpi, ReactNode> = {
    accounts: <Tile label={t("dashboard.accounts")} value={d.accounts} />,
    protected: (
      <Tile
        label={t("dashboard.protected")}
        value={d.protected_24h}
        tone="ok"
      />
    ),
    never: (
      <Tile
        label={t("dashboard.never")}
        value={d.never_backed_up}
        tone={d.never_backed_up ? "warn" : undefined}
      />
    ),
    partial: (
      <Tile
        label={t("dashboard.partial")}
        value={d.partial_last}
        tone={d.partial_last ? "warn" : undefined}
      />
    ),
    queued: <Tile label={t("dashboard.queued")} value={d.jobs_queued} />,
    running: <Tile label={t("dashboard.running")} value={d.jobs_running} />,
    failed: (
      <Tile
        label={t("dashboard.failed")}
        value={d.jobs_failed_24h}
        tone={d.jobs_failed_24h ? "bad" : undefined}
      />
    ),
    stored: (
      <Tile label={t("dashboard.stored")} value={formatBytes(d.stored_bytes)} />
    ),
    agents:
      d.agents_active !== null ? (
        <Tile
          label={t("dashboard.agents")}
          value={d.agents_active}
          tone={d.agents_active ? "ok" : "bad"}
        />
      ) : null,
    mail_volume: (
      <Tile
        label={t("dashboard.mailVolume")}
        value={formatBytes(d.mail_used_bytes ?? 0)}
      />
    ),
    archived: (
      <Tile
        label={t("widgets.kpi.archived")}
        value={formatBytes(d.archived_bytes ?? 0)}
      />
    ),
  };
  return (
    <>
      <section className="tiles" aria-label={t("dashboard.title")}>
        {items.map((k) => (
          <Fragment key={k}>{tiles[k]}</Fragment>
        ))}
      </section>
      {d.last_agent_heartbeat && (
        <p className="muted">
          {t("dashboard.heartbeat", {
            when: formatRelative(d.last_agent_heartbeat),
          })}
        </p>
      )}
    </>
  );
}

function JobsWidget({ w, refresh }: { w: Widget; refresh: number }) {
  const count = opt(w, "count", 10);
  const status = opt(w, "status", "");
  const kind = opt(w, "kind", "");
  const [jobs, reload] = useLoad<Page<Job>>(
    () =>
      api.jobs({
        limit: count,
        status: (status || undefined) as JobStatus | undefined,
        kind: kind || undefined,
      }),
    [count, status, kind, refresh],
    5000,
  );
  return (
    <LoadView load={jobs} reload={reload}>
      {(page) => <JobTable jobs={page.items.slice(0, count)} />}
    </LoadView>
  );
}

function PoliciesWidget() {
  const { t } = useTranslation();
  const [policies, reload] = useLoad<Policy[]>(() => api.policies(), [], 30000);
  return (
    <LoadView load={policies} reload={reload}>
      {(list) =>
        list.length === 0 ? (
          <p className="muted">{t("policies.none")}</p>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>{t("policies.name")}</th>
                  <th>{t("policies.last")}</th>
                  <th>{t("policies.next")}</th>
                  <th className="num">{t("policies.volume")}</th>
                </tr>
              </thead>
              <tbody>
                {list.map((p) => (
                  <tr key={p.id}>
                    <td>
                      <Link to="/policies">{p.name}</Link>
                    </td>
                    <td>
                      {p.last_run ? (
                        <>
                          <StatusBadge status={p.last_run.status} />{" "}
                          <span className="muted small">
                            {formatDate(p.last_run.started_at)}
                          </span>
                        </>
                      ) : (
                        <span className="muted">{t("accounts.never")}</span>
                      )}
                    </td>
                    <td>
                      {p.enabled && p.next_run_at
                        ? formatRelative(p.next_run_at)
                        : "—"}
                    </td>
                    <td className="num">
                      {formatBytes(p.protected_bytes ?? 0)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )
      }
    </LoadView>
  );
}

function MailTopWidget({ w }: { w: Widget }) {
  const { t } = useTranslation();
  const period = opt<MailPeriod>(w, "period", "24h");
  const count = opt(w, "count", 10);
  const [load, reload] = useLoad<MailOverview>(
    () => api.mailOverview(period),
    [period],
    60000,
  );
  return (
    <LoadView load={load} reload={reload}>
      {(d) =>
        d.top_senders.length === 0 ? (
          <p className="muted">{t("widgets.noMail")}</p>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>{t("mail.sender")}</th>
                  <th className="num">{t("widgets.messages")}</th>
                  <th className="num">{t("widgets.recipients")}</th>
                </tr>
              </thead>
              <tbody>
                {d.top_senders.slice(0, count).map((s) => (
                  <tr key={s.email}>
                    <td className="wrap">{s.email}</td>
                    <td className="num">{s.messages.toLocaleString()}</td>
                    <td className="num">{s.recipients.toLocaleString()}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )
      }
    </LoadView>
  );
}

function WidgetBody({
  w,
  stats,
  reloadStats,
  onQueued,
  refresh,
}: {
  w: Widget;
  stats: ReturnType<typeof useLoad<Dashboard>>[0];
  reloadStats: () => void;
  onQueued: () => void;
  refresh: number;
}) {
  const { t } = useTranslation();
  switch (w.type) {
    case "kpis": {
      const items = opt<string[]>(w, "items", [...KPIS]).filter((k): k is Kpi =>
        (KPIS as readonly string[]).includes(k),
      );
      return (
        <LoadView load={stats} reload={reloadStats}>
          {(d) => <KpiTiles d={d} items={items} />}
        </LoadView>
      );
    }
    case "storage":
      return (
        <LoadView load={stats} reload={reloadStats}>
          {(d) => <StorageOverview d={d} />}
        </LoadView>
      );
    case "manual_backup":
      return <ManualBackup onQueued={onQueued} />;
    case "jobs":
      return <JobsWidget w={w} refresh={refresh} />;
    case "policies":
      return <PoliciesWidget />;
    case "mail_top":
      return <MailTopWidget w={w} />;
    case "spam_sources":
      return (
        <SpamSourcesPanel
          period={opt<MailPeriod>(w, "period", "24h")}
          limit={opt(w, "count", 10)}
          compact={w.size === "half"}
        />
      );
    case "blocked_ips":
      return (
        <BlockedIpsPanel
          period={opt<MailPeriod>(w, "period", "24h")}
          onlyBlocked={opt(w, "only_blocked", false)}
          limit={opt(w, "count", 10)}
          compact={w.size === "half"}
        />
      );
    default:
      return <p className="muted">{t("widgets.unknown")}</p>;
  }
}

/** Options of one widget, edited in place. */
function WidgetOptions({
  w,
  onChange,
}: {
  w: Widget;
  onChange: (options: Record<string, unknown>) => void;
}) {
  const { t } = useTranslation();
  const set = (key: string, value: unknown) =>
    onChange({ ...w.options, [key]: value });
  const select = (key: string, values: (string | number)[], label: string) => (
    <label>
      {label}
      <select
        value={String(w.options[key] ?? "")}
        onChange={(e) =>
          set(
            key,
            typeof values[0] === "number"
              ? Number(e.target.value)
              : e.target.value,
          )
        }
      >
        {values.map((v) => (
          <option key={v} value={v}>
            {key === "period"
              ? t(`mail.periods.${v}`)
              : key === "status"
                ? v
                  ? t(`status.${v}`)
                  : t("jobs.all")
                : key === "kind"
                  ? v
                    ? t(`kind.${v}`)
                    : t("jobs.allKinds")
                  : v}
          </option>
        ))}
      </select>
    </label>
  );
  switch (w.type) {
    case "kpis": {
      const items = opt<string[]>(w, "items", [...KPIS]);
      return (
        <fieldset className="choices">
          <legend>{t("widgets.shownFigures")}</legend>
          {KPIS.map((k) => (
            <label key={k} className="check">
              <input
                type="checkbox"
                checked={items.includes(k)}
                onChange={() =>
                  set(
                    "items",
                    items.includes(k)
                      ? items.filter((x) => x !== k)
                      : KPIS.filter((x) => x === k || items.includes(x)),
                  )
                }
              />
              {t(`widgets.kpi.${k}`)}
            </label>
          ))}
        </fieldset>
      );
    }
    case "jobs":
      return (
        <div className="row">
          {select("count", COUNTS, t("widgets.count"))}
          {select("status", JOB_STATUSES, t("jobs.filter"))}
          {select("kind", JOB_KINDS, t("jobs.kindFilter"))}
        </div>
      );
    case "mail_top":
    case "spam_sources":
      return (
        <div className="row">
          {select("period", PERIODS, t("mail.period"))}
          {select("count", COUNTS, t("widgets.count"))}
        </div>
      );
    case "blocked_ips":
      return (
        <div className="row">
          {select("period", PERIODS, t("mail.period"))}
          {select("count", COUNTS, t("widgets.count"))}
          <label className="check">
            <input
              type="checkbox"
              checked={opt(w, "only_blocked", false)}
              onChange={(e) => set("only_blocked", e.target.checked)}
            />
            {t("widgets.onlyBlocked")}
          </label>
        </div>
      );
    default:
      return <p className="muted small">{t("widgets.noOptions")}</p>;
  }
}

function newId(type: WidgetType, taken: Widget[]): string {
  let n = 1;
  while (taken.some((w) => w.id === `${type.replace("_", "-")}-${n}`)) n++;
  return `${type.replace("_", "-")}-${n}`;
}

export function DashboardPage() {
  const { t } = useTranslation();
  const { canManageSettings } = useAuth();
  const [stats, reloadStats] = useLoad<Dashboard>(
    () => api.dashboard(),
    [],
    15000,
  );
  const [policies] = useLoad<Policy[]>(() => api.policies(), []);
  const [blocked] = useLoad<LoginSecurity | null>(
    () =>
      canManageSettings
        ? api.mailSecurity("24h", true, 20)
        : Promise.resolve(null),
    [canManageSettings],
    60000,
  );
  const blockedNow =
    blocked.state === "ready" && blocked.data
      ? blocked.data.ips.filter((ip) => ip.recently_blocked && !ip.trusted)
      : [];
  const [saved, setSaved] = useState<Widget[] | null | undefined>(undefined);
  const [draft, setDraft] = useState<Widget[] | null>(null);
  const [adding, setAdding] = useState<WidgetType>("jobs");
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    api
      .dashboardLayout()
      .then((r) => setSaved(r.widgets))
      .catch(() => setSaved(null)); // the default layout still works
  }, []);

  const allowed = (type: WidgetType) =>
    canManageSettings || !ADMIN_WIDGETS.has(type);
  const layout = (draft ?? saved ?? defaultLayout(canManageSettings)).filter(
    (w) => allowed(w.type),
  );
  const editing = draft !== null;

  const change = (id: string, patch: Partial<Widget>) =>
    setDraft((d) =>
      (d ?? layout).map((w) => (w.id === id ? { ...w, ...patch } : w)),
    );
  const move = (index: number, delta: number) =>
    setDraft((d) => {
      const list = [...(d ?? layout)];
      const [item] = list.splice(index, 1);
      if (item) list.splice(Math.max(0, index + delta), 0, item);
      return list;
    });

  async function save(widgets: Widget[] | null) {
    setError(null);
    try {
      const r = await api.saveDashboardLayout(widgets);
      setSaved(r.widgets);
      setDraft(null);
    } catch (err) {
      setError(t("error", { message: errorMessage(err) }));
    }
  }

  return (
    <>
      <div className="page-head">
        <h1>{t("dashboard.title")}</h1>
        {!editing ? (
          <button
            type="button"
            className="secondary"
            disabled={saved === undefined}
            onClick={() => setDraft(layout)}
          >
            {t("widgets.customize")}
          </button>
        ) : (
          <span className="actions">
            <button type="button" onClick={() => void save(draft)}>
              {t("widgets.save")}
            </button>
            <button
              type="button"
              className="secondary"
              onClick={() => {
                if (window.confirm(t("widgets.confirmReset"))) void save(null);
              }}
            >
              {t("widgets.reset")}
            </button>
            <button
              type="button"
              className="secondary"
              onClick={() => setDraft(null)}
            >
              {t("widgets.cancel")}
            </button>
          </span>
        )}
      </div>
      {stats.state === "ready" &&
        (stats.data.services_down?.length ?? 0) > 0 && (
          <p role="alert" className="alert error">
            {t("alerts.servicesDown", {
              list: stats.data.services_down?.join(", "),
            })}{" "}
            <Link to="/monitoring">{t("alerts.seeMonitoring")}</Link>
          </p>
        )}
      {stats.state === "ready" && (stats.data.blacklisted?.length ?? 0) > 0 && (
        <p role="alert" className="alert error">
          {t("alerts.blacklisted", {
            list: stats.data.blacklisted?.join(", "),
          })}{" "}
          <Link to="/monitoring">{t("alerts.seeMonitoring")}</Link>
        </p>
      )}
      {blockedNow.length > 0 && (
        <p role="alert" className="alert error">
          {t("alerts.ipBlocked", {
            count: blockedNow.length,
            list: blockedNow.map((ip) => ip.ip).join(", "),
          })}{" "}
          <Link to="/mail">{t("alerts.seeBlocked")}</Link>
        </p>
      )}
      {policies.state === "ready" && !policies.data.some((p) => p.enabled) && (
        <p role="alert" className="alert warn">
          {t("alerts.noPolicy")}{" "}
          <Link to="/policies">{t("alerts.createPolicy")}</Link>
        </p>
      )}
      {stats.state === "ready" &&
        stats.data.accounts - stats.data.protected_24h > 0 && (
          <p role="alert" className="alert warn">
            {t("alerts.unprotected", {
              count: stats.data.accounts - stats.data.protected_24h,
            })}{" "}
            <Link to="/accounts">{t("alerts.seeAccounts")}</Link>
          </p>
        )}
      {error && (
        <p role="alert" className="alert error">
          {error}
        </p>
      )}
      {editing && (
        <div className="card row widget-add">
          <label>
            {t("widgets.add")}
            <select
              value={adding}
              onChange={(e) => setAdding(e.target.value as WidgetType)}
            >
              {WIDGET_TYPES.filter(allowed).map((type) => (
                <option key={type} value={type}>
                  {t(`widgets.types.${type}`)}
                </option>
              ))}
            </select>
          </label>
          <button
            type="button"
            className="align-end"
            onClick={() =>
              setDraft((d) => {
                const list = d ?? layout;
                return [
                  ...list,
                  {
                    id: newId(adding, list),
                    type: adding,
                    size: "full",
                    options: defaultOptions(adding),
                  },
                ];
              })
            }
          >
            {t("widgets.addButton")}
          </button>
          <p className="muted small grow">{t("widgets.help")}</p>
        </div>
      )}
      {layout.length === 0 && <p className="muted">{t("widgets.empty")}</p>}
      <div className="dash-grid">
        {layout.map((w, index) => {
          const title = w.title || t(`widgets.types.${w.type}`);
          return (
            <section
              key={w.id}
              className={`widget ${w.size === "half" ? "half" : "full"}${editing ? " editing" : ""}`}
              aria-label={title}
            >
              <div className="widget-head">
                <h2>{title}</h2>
                {editing && (
                  <span className="actions">
                    <button
                      type="button"
                      className="small secondary"
                      disabled={index === 0}
                      aria-label={t("widgets.up", { title })}
                      onClick={() => move(index, -1)}
                    >
                      ↑
                    </button>
                    <button
                      type="button"
                      className="small secondary"
                      disabled={index === layout.length - 1}
                      aria-label={t("widgets.down", { title })}
                      onClick={() => move(index, 1)}
                    >
                      ↓
                    </button>
                    <button
                      type="button"
                      className="small secondary"
                      onClick={() =>
                        change(w.id, {
                          size: w.size === "half" ? "full" : "half",
                        })
                      }
                    >
                      {w.size === "half"
                        ? t("widgets.fullWidth")
                        : t("widgets.halfWidth")}
                    </button>
                    <button
                      type="button"
                      className="small secondary"
                      aria-label={t("widgets.remove", { title })}
                      onClick={() =>
                        setDraft((d) =>
                          (d ?? layout).filter((x) => x.id !== w.id),
                        )
                      }
                    >
                      {t("widgets.removeShort")}
                    </button>
                  </span>
                )}
              </div>
              {editing && (
                <div className="widget-options">
                  <label>
                    {t("widgets.title")}
                    <input
                      value={w.title ?? ""}
                      maxLength={80}
                      placeholder={t(`widgets.types.${w.type}`)}
                      onChange={(e) =>
                        change(w.id, { title: e.target.value || null })
                      }
                    />
                  </label>
                  <WidgetOptions
                    w={w}
                    onChange={(options) => change(w.id, { options })}
                  />
                </div>
              )}
              <WidgetBody
                w={w}
                refresh={tick}
                stats={stats}
                reloadStats={reloadStats}
                onQueued={() => setTick((n) => n + 1)}
              />
            </section>
          );
        })}
      </div>
    </>
  );
}
