import { useState } from "react";
import type { FormEvent } from "react";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import type { TFunction } from "i18next";

import { api } from "../api/endpoints";
import type {
  IntegrityCheck,
  MonitoringOverview,
  MonitoringSettings,
  StorageSeries,
} from "../api/types";
import { useAuth } from "../auth/AuthContext";
import {
  LoadView,
  StatusBadge,
  errorMessage,
  useLoad,
} from "../components/common";
import { NoticeBar, type Notice } from "../components/notices";
import { formatBytes, formatDate, formatRelative } from "../utils/format";

const RISKS = ["never", "stale", "failed", "partial", "no_policy"];
const DAY = 86400;

function duration(seconds: number, t: TFunction) {
  if (seconds < 3600)
    return t("monitoring.minutes", { n: Math.round(seconds / 60) });
  if (seconds < 2 * DAY)
    return t("monitoring.hours", { n: Math.round(seconds / 3600) });
  return t("monitoring.days", { n: Math.round(seconds / DAY) });
}

/** Used space over time, with the total as a dashed line and the forecast. */
function StorageChart({ series }: { series: StorageSeries }) {
  const { t } = useTranslation();
  const w = 600;
  const h = 140;
  const pts = series.points;
  const first = pts.at(0);
  const last = pts.at(-1);
  const t0 = first ? new Date(first.at).getTime() : 0;
  const t1 = last ? new Date(last.at).getTime() : 1;
  const span = Math.max(1, t1 - t0);
  const x = (at: string) => ((new Date(at).getTime() - t0) / span) * w;
  const y = (bytes: number) =>
    h - (bytes / Math.max(1, series.total_bytes)) * h;
  const path = pts
    .map(
      (p, i) =>
        `${i ? "L" : "M"}${x(p.at).toFixed(1)},${y(p.used_bytes).toFixed(1)}`,
    )
    .join(" ");
  const pct = Math.round(
    (series.used_bytes / Math.max(1, series.total_bytes)) * 100,
  );
  const days = series.days_until_full;
  const tone = days !== null && days < 14 ? "bad" : pct >= 85 ? "warn" : "ok";
  return (
    <div className="card stack">
      <h3>
        {t(`monitoring.target.${series.target}`)} — {series.agent}
      </h3>
      <p>
        <span className={`badge ${tone}`}>
          {t("monitoring.used", {
            used: formatBytes(series.used_bytes),
            total: formatBytes(series.total_bytes),
            pct,
          })}
        </span>{" "}
        {days !== null ? (
          <span className={days < 14 ? "bad-text" : ""}>
            {t("monitoring.fullIn", { days: Math.round(days) })}
          </span>
        ) : (
          <span className="muted">{t("monitoring.notGrowing")}</span>
        )}
        {series.growth_per_day !== null && series.growth_per_day > 0 && (
          <span className="muted">
            {" "}
            ·{" "}
            {t("monitoring.growth", {
              size: formatBytes(series.growth_per_day),
            })}
          </span>
        )}
      </p>
      {pts.length > 1 ? (
        <svg
          className="spark"
          viewBox={`0 0 ${w} ${h}`}
          preserveAspectRatio="none"
          role="img"
          aria-label={t("monitoring.chartLabel", {
            target: t(`monitoring.target.${series.target}`),
          })}
        >
          <line x1="0" y1="1" x2={w} y2="1" className="spark-total" />
          <path d={path} className="spark-line" />
        </svg>
      ) : (
        <p className="muted">{t("monitoring.notEnough")}</p>
      )}
    </div>
  );
}

function Integrity({
  label,
  check,
}: {
  label: string;
  check: IntegrityCheck | null;
}) {
  const { t } = useTranslation();
  if (!check)
    return (
      <li>
        <strong>{label}</strong> :{" "}
        <span className="muted">{t("monitoring.neverChecked")}</span>
      </li>
    );
  const p = check.progress ?? {};
  const bad = (p.corrupted_count ?? 0) + (p.manifests_corrupted?.length ?? 0);
  return (
    <li>
      <strong>{label}</strong> : <StatusBadge status={check.status} />{" "}
      {formatDate(check.finished_at ?? check.created_at)}
      {check.status === "succeeded" && (
        <>
          {" — "}
          {t("monitoring.checked", {
            n: (p.checked ?? 0) + (p.manifests_checked ?? 0),
            pct: Math.round((check.sample ?? 1) * 100),
          })}{" "}
          {bad ? (
            <span className="badge bad">
              {t("monitoring.corrupted", { n: bad })}
            </span>
          ) : (
            <span className="badge ok">{t("monitoring.intact")}</span>
          )}
        </>
      )}
      {check.status === "failed" && check.error && (
        <span className="bad-text"> — {check.error}</span>
      )}
    </li>
  );
}

function SettingsForm({
  initial,
  onSaved,
}: {
  initial: MonitoringSettings;
  onSaved: () => void;
}) {
  const { t } = useTranslation();
  const [form, setForm] = useState(initial);
  const [notice, setNotice] = useState<Notice>(null);
  const set = <K extends keyof MonitoringSettings>(
    k: K,
    v: MonitoringSettings[K],
  ) => setForm((f) => ({ ...f, [k]: v }));

  async function submit(event: FormEvent) {
    event.preventDefault();
    try {
      await api.saveMonitoringSettings(form);
      setNotice({ kind: "ok", text: t("monitoring.saved") });
      onSaved();
    } catch (err) {
      setNotice({
        kind: "error",
        text: t("error", { message: errorMessage(err) }),
      });
    }
  }

  return (
    <form className="card stack" onSubmit={(e) => void submit(e)}>
      <h2>{t("monitoring.settings")}</h2>
      <div className="row">
        <label>
          {t("monitoring.staleHours")}
          <input
            type="number"
            min={1}
            value={form.stale_hours}
            onChange={(e) => set("stale_hours", Number(e.target.value))}
          />
        </label>
        <label>
          {t("monitoring.lagHours")}
          <input
            type="number"
            min={1}
            value={form.replica_max_lag_hours}
            onChange={(e) =>
              set("replica_max_lag_hours", Number(e.target.value))
            }
          />
        </label>
      </div>
      <label className="inline">
        <input
          type="checkbox"
          checked={form.integrity_enabled}
          onChange={(e) => set("integrity_enabled", e.target.checked)}
        />
        {t("monitoring.integrityEnabled")}
      </label>
      <div className="row">
        <label className="grow">
          {t("monitoring.integrityCron")}
          <input
            value={form.integrity_cron}
            onChange={(e) => set("integrity_cron", e.target.value)}
          />
        </label>
        <label>
          {t("monitoring.integritySample")}
          <input
            type="number"
            min={1}
            max={100}
            value={Math.round(form.integrity_sample * 100)}
            onChange={(e) =>
              set("integrity_sample", Number(e.target.value) / 100)
            }
          />
        </label>
        <label>
          {t("monitoring.timezone")}
          <input
            value={form.integrity_timezone}
            onChange={(e) => set("integrity_timezone", e.target.value)}
          />
        </label>
      </div>
      <p className="muted">{t("monitoring.cronHelp")}</p>
      <NoticeBar notice={notice} />
      <div className="actions">
        <button type="submit">{t("save")}</button>
      </div>
    </form>
  );
}

export function MonitoringPage() {
  const { t } = useTranslation();
  const { canManageSettings } = useAuth();
  const [load, reload] = useLoad<MonitoringOverview>(
    () => api.monitoring(),
    [],
    60000,
  );
  const [notice, setNotice] = useState<Notice>(null);
  const [showSettings, setShowSettings] = useState(false);

  if (!canManageSettings) {
    return (
      <p role="alert" className="alert error">
        {t("settings.forbidden")}
      </p>
    );
  }

  async function verify(target: "primary" | "replica", sample: number) {
    setNotice(null);
    try {
      const r = await api.verifyNow(target, sample);
      setNotice({
        kind: "ok",
        text: t(
          r.queued ? "monitoring.verifyQueued" : "monitoring.verifyPending",
        ),
      });
      reload();
    } catch (err) {
      setNotice({
        kind: "error",
        text: t("error", { message: errorMessage(err) }),
      });
    }
  }

  return (
    <>
      <div className="toolbar">
        <h1 className="grow">{t("monitoring.title")}</h1>
        <button
          type="button"
          className="secondary"
          onClick={() => setShowSettings((v) => !v)}
        >
          {t("monitoring.settings")}
        </button>
      </div>
      <NoticeBar notice={notice} />
      <LoadView load={load} reload={reload}>
        {(d) => {
          const risky =
            (d.at_risk.never?.count ?? 0) + (d.at_risk.stale?.count ?? 0);
          const failed = Object.values(d.jobs_failed_24h).reduce(
            (a, b) => a + b,
            0,
          );
          const soonest = d.storage
            .map((s) => s.days_until_full)
            .filter((v): v is number => v !== null)
            .sort((a, b) => a - b)[0];
          const corrupted = [d.integrity.primary, d.integrity.replica].reduce(
            (n, c) =>
              n +
              (c?.progress?.corrupted_count ?? 0) +
              (c?.progress?.manifests_corrupted?.length ?? 0),
            0,
          );
          return (
            <>
              {showSettings && (
                <SettingsForm
                  initial={d.settings}
                  onSaved={() => {
                    setShowSettings(false);
                    reload();
                  }}
                />
              )}
              <section className="tiles" aria-label={t("monitoring.health")}>
                <div className={`tile ${risky ? "bad" : "ok"}`}>
                  <span className="tile-value">{risky}</span>
                  <span className="tile-label">{t("monitoring.atRisk")}</span>
                </div>
                <div className={`tile ${failed ? "bad" : "ok"}`}>
                  <span className="tile-value">{failed}</span>
                  <span className="tile-label">
                    {t("monitoring.failed24h")}
                  </span>
                </div>
                <div
                  className={`tile ${soonest !== undefined && soonest < 14 ? "bad" : soonest !== undefined && soonest < 60 ? "warn" : "ok"}`}
                >
                  <span className="tile-value">
                    {soonest !== undefined ? Math.round(soonest) : "∞"}
                  </span>
                  <span className="tile-label">
                    {t("monitoring.daysToFull")}
                  </span>
                </div>
                <div className={`tile ${d.replica?.late ? "bad" : "ok"}`}>
                  <span className="tile-value">
                    {d.replica
                      ? d.replica.behind
                        ? duration(d.replica.lag_seconds, t)
                        : t("monitoring.upToDate")
                      : "—"}
                  </span>
                  <span className="tile-label">{t("monitoring.copy2Lag")}</span>
                </div>
                <div className={`tile ${corrupted ? "bad" : "ok"}`}>
                  <span className="tile-value">{corrupted}</span>
                  <span className="tile-label">
                    {t("monitoring.corruptedFiles")}
                  </span>
                </div>
              </section>

              <h2>{t("monitoring.storage")}</h2>
              {d.storage.length === 0 ? (
                <p className="muted">{t("monitoring.noStorage")}</p>
              ) : (
                <div className="split">
                  {d.storage.map((s) => (
                    <StorageChart key={`${s.agent}-${s.target}`} series={s} />
                  ))}
                </div>
              )}

              <h2>{t("monitoring.riskTitle")}</h2>
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>{t("monitoring.reason")}</th>
                      <th className="num">{t("monitoring.count")}</th>
                      <th>{t("monitoring.examples")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {RISKS.map((key) => {
                      const cat = d.at_risk[key];
                      if (!cat) return null;
                      const link =
                        "/accounts?" +
                        new URLSearchParams(
                          cat.filters.map((f) => ["f", f]),
                        ).toString();
                      return (
                        <tr key={key}>
                          <td>
                            {t(`monitoring.risk.${key}`, {
                              hours: d.settings.stale_hours,
                            })}
                          </td>
                          <td className={`num ${cat.count ? "bad-text" : ""}`}>
                            {cat.filters.length && cat.count ? (
                              <Link to={link}>{cat.count}</Link>
                            ) : (
                              cat.count
                            )}
                          </td>
                          <td className="wrap">
                            {cat.accounts.slice(0, 5).map((a, i) => (
                              <span key={a.id}>
                                {i > 0 && ", "}
                                <Link to={`/accounts/${a.id}`}>{a.email}</Link>
                              </span>
                            ))}
                            {cat.count > 5 && <span className="muted"> …</span>}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>

              <div className="split">
                <section className="card stack">
                  <h2>{t("monitoring.copy2")}</h2>
                  {d.replica ? (
                    <>
                      <p>
                        {d.replica.last_success_at
                          ? t("monitoring.lastCopy", {
                              when: formatRelative(d.replica.last_success_at),
                            })
                          : t("monitoring.neverCopied")}{" "}
                        {d.replica.late ? (
                          <span className="badge bad">
                            {t("monitoring.late", {
                              hours: d.replica.max_lag_hours,
                            })}
                          </span>
                        ) : d.replica.behind ? (
                          <span className="badge warn">
                            {t("monitoring.behind")}
                          </span>
                        ) : (
                          <span className="badge ok">
                            {t("monitoring.upToDate")}
                          </span>
                        )}
                      </p>
                      {d.replica.last_status === "failed" &&
                        d.replica.last_error && (
                          <p className="alert error">{d.replica.last_error}</p>
                        )}
                    </>
                  ) : (
                    <p className="muted">
                      {t("monitoring.noCopy2")}{" "}
                      <Link to="/settings">{t("nav.settings")}</Link>
                    </p>
                  )}
                </section>
                <section className="card stack">
                  <h2>{t("monitoring.integrity")}</h2>
                  <ul>
                    <Integrity
                      label={t("monitoring.target.primary")}
                      check={d.integrity.primary}
                    />
                    {d.replica && (
                      <Integrity
                        label={t("monitoring.target.replica")}
                        check={d.integrity.replica}
                      />
                    )}
                  </ul>
                  <p className="muted">
                    {d.settings.integrity_enabled && d.next_integrity_at
                      ? t("monitoring.nextCheck", {
                          when: formatDate(d.next_integrity_at),
                          pct: Math.round(d.settings.integrity_sample * 100),
                        })
                      : t("monitoring.noSchedule")}
                  </p>
                  <div className="actions">
                    <button
                      type="button"
                      className="small"
                      onClick={() => void verify("primary", 1)}
                    >
                      {t("monitoring.verifyPrimary")}
                    </button>
                    {d.replica && (
                      <button
                        type="button"
                        className="small secondary"
                        onClick={() => void verify("replica", 1)}
                      >
                        {t("monitoring.verifyReplica")}
                      </button>
                    )}
                  </div>
                </section>
              </div>

              <h2>{t("monitoring.agents")}</h2>
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>{t("monitoring.agent")}</th>
                      <th>{t("monitoring.version")}</th>
                      <th>{t("monitoring.lastSeen")}</th>
                      <th>{t("monitoring.certificate")}</th>
                      <th>{t("monitoring.mailStats")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {d.agents.map((a) => {
                      const silent =
                        !a.last_heartbeat_at ||
                        Date.now() - new Date(a.last_heartbeat_at).getTime() >
                          30 * 60 * 1000;
                      const certDays = a.cert_not_after
                        ? (new Date(a.cert_not_after).getTime() - Date.now()) /
                          1000 /
                          DAY
                        : null;
                      return (
                        <tr key={a.name}>
                          <td>{a.name}</td>
                          <td>{a.version ?? "—"}</td>
                          <td className={silent ? "bad-text" : ""}>
                            {a.last_heartbeat_at
                              ? formatRelative(a.last_heartbeat_at)
                              : "—"}
                          </td>
                          <td
                            className={
                              certDays !== null && certDays < 30
                                ? "warn-text"
                                : ""
                            }
                          >
                            {a.cert_not_after
                              ? formatDate(a.cert_not_after)
                              : "—"}
                          </td>
                          <td>
                            {a.mail_reported_at
                              ? formatRelative(a.mail_reported_at)
                              : "—"}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
              <p className="muted">
                {d.metrics_token_configured
                  ? t("monitoring.prometheus")
                  : t("monitoring.prometheusNoToken")}
              </p>
            </>
          );
        }}
      </LoadView>
    </>
  );
}
