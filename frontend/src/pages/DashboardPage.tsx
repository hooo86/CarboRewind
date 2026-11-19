import { useState } from "react";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";

import { api } from "../api/endpoints";
import type {
  BackupType,
  Dashboard,
  Domain,
  Job,
  Page,
  Policy,
} from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { JobTable } from "../components/JobTable";
import { LoadView, errorMessage, useLoad } from "../components/common";
import { formatBytes, formatRelative } from "../utils/format";

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
    <section className="card stack" aria-labelledby="storage-title">
      <h2 id="storage-title">{t("dashboard.storageTitle")}</h2>
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
    </section>
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
  if (allowed.length === 0) return null;

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
    <section className="card stack" aria-labelledby="manual-title">
      <h2 id="manual-title">{t("manual.title")}</h2>
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
    </section>
  );
}

export function DashboardPage() {
  const { t } = useTranslation();
  const [stats, reloadStats] = useLoad<Dashboard>(
    () => api.dashboard(),
    [],
    15000,
  );
  const [jobs, reloadJobs] = useLoad<Page<Job>>(() => api.jobs(), [], 5000);
  const [policies] = useLoad<Policy[]>(() => api.policies(), []);

  return (
    <>
      <h1>{t("dashboard.title")}</h1>
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
      <LoadView load={stats} reload={reloadStats}>
        {(d) => (
          <>
            <section className="tiles" aria-label={t("dashboard.title")}>
              <Tile label={t("dashboard.accounts")} value={d.accounts} />
              <Tile
                label={t("dashboard.protected")}
                value={d.protected_24h}
                tone="ok"
              />
              <Tile
                label={t("dashboard.never")}
                value={d.never_backed_up}
                tone={d.never_backed_up ? "warn" : undefined}
              />
              <Tile
                label={t("dashboard.partial")}
                value={d.partial_last}
                tone={d.partial_last ? "warn" : undefined}
              />
              <Tile label={t("dashboard.queued")} value={d.jobs_queued} />
              <Tile label={t("dashboard.running")} value={d.jobs_running} />
              <Tile
                label={t("dashboard.failed")}
                value={d.jobs_failed_24h}
                tone={d.jobs_failed_24h ? "bad" : undefined}
              />
              <Tile
                label={t("dashboard.stored")}
                value={formatBytes(d.stored_bytes)}
              />
              {d.agents_active !== null && (
                <Tile
                  label={t("dashboard.agents")}
                  value={d.agents_active}
                  tone={d.agents_active ? "ok" : "bad"}
                />
              )}
            </section>
            {d.last_agent_heartbeat && (
              <p className="muted">
                {t("dashboard.heartbeat", {
                  when: formatRelative(d.last_agent_heartbeat),
                })}
              </p>
            )}
            <StorageOverview d={d} />
          </>
        )}
      </LoadView>
      <ManualBackup onQueued={reloadJobs} />
      <h2>{t("dashboard.recentJobs")}</h2>
      <LoadView load={jobs} reload={reloadJobs}>
        {(page) => <JobTable jobs={page.items.slice(0, 10)} />}
      </LoadView>
    </>
  );
}
