import { useState } from "react";
import { useTranslation } from "react-i18next";

import { api } from "../api/endpoints";
import type {
  LoginFilter,
  LoginSecurity,
  MailPeriod,
  SpamSources,
} from "../api/types";
import { formatDate, formatRelative } from "../utils/format";
import { LoadView, StatusBadge, errorMessage, useLoad } from "./common";

/** Approximate end of a block: last suspension + Carbonio's blocking delay. */
function blockedUntil(
  last: string | null,
  minutes: number | null | undefined,
): Date | null {
  if (!last || !minutes) return null;
  return new Date(new Date(last).getTime() + minutes * 60_000);
}

/** Trust (never block again) or untrust one address; asks first. */
function IpButton({
  ip,
  action,
  label,
  onDone,
}: {
  ip: string;
  action: "trust" | "untrust";
  label: string;
  onDone: (error: string | null) => void;
}) {
  const { t } = useTranslation();
  const [busy, setBusy] = useState(false);
  return (
    <button
      type="button"
      className={`small${action === "trust" ? "" : " secondary"}`}
      disabled={busy}
      aria-label={t(`loginSecurity.${action}Title`, { ip })}
      onClick={() => {
        if (!window.confirm(t(`loginSecurity.${action}Confirm`, { ip })))
          return;
        setBusy(true);
        api
          .ipAction(action, ip)
          .then(() => onDone(null))
          .catch((err: unknown) => onDone(errorMessage(err)))
          .finally(() => setBusy(false));
      }}
    >
      {label}
    </button>
  );
}

/** IPs with failed logins, and those Carbonio blocked after repeated failures. */
export function BlockedIpsPanel({
  period,
  onlyBlocked = false,
  limit = 100,
  compact = false,
  onChange,
}: {
  period: MailPeriod;
  onlyBlocked?: boolean;
  limit?: number;
  compact?: boolean;
  onChange?: () => void;
}) {
  const { t } = useTranslation();
  const [load, reload] = useLoad<LoginSecurity>(
    () => api.mailSecurity(period, onlyBlocked, limit),
    [period, onlyBlocked, limit],
    60000,
  );
  const [filter] = useLoad<LoginFilter>(() => api.loginFilter(), [], 60000);
  const [notice, setNotice] = useState<{ ok: boolean; text: string } | null>(
    null,
  );
  const blockMinutes =
    filter.state === "ready" ? filter.data.config?.block_minutes : null;
  const done =
    (ip: string, action: "trust" | "untrust") => (error: string | null) => {
      setNotice(
        error
          ? { ok: false, text: t("error", { message: error }) }
          : { ok: true, text: t(`loginSecurity.${action}Queued`, { ip }) },
      );
      reload();
      onChange?.();
    };
  return (
    <LoadView load={load} reload={reload}>
      {(d) => (
        <div className="stack">
          <p className="muted small">
            {t("loginSecurity.totals", {
              ips: d.totals.ips,
              failed: d.totals.failed,
              blocked: d.totals.blocked,
            })}
          </p>
          {d.log_status && d.log_status !== "ok" ? (
            <p role="alert" className="alert warn small">
              {t(`loginSecurity.logStatus.${d.log_status}`, {
                path: d.log_path ?? "/opt/zextras/log/mailbox.log",
              })}
            </p>
          ) : (
            d.totals.blocked === 0 && (
              <p role="status" className="alert ok small">
                {t("loginSecurity.noBlock")}
              </p>
            )
          )}
          {notice && (
            <p
              role={notice.ok ? "status" : "alert"}
              className={`alert small ${notice.ok ? "ok" : "error"}`}
            >
              {notice.text}
            </p>
          )}
          {d.ips.length === 0 ? (
            !onlyBlocked && <p className="muted">{t("loginSecurity.none")}</p>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>{t("loginSecurity.ip")}</th>
                    <th>{t("loginSecurity.state")}</th>
                    <th className="num">{t("loginSecurity.failed")}</th>
                    {!compact && <th>{t("loginSecurity.accounts")}</th>}
                    <th>{t("loginSecurity.lastSeen")}</th>
                    <th>{t("accounts.actions")}</th>
                  </tr>
                </thead>
                <tbody>
                  {d.ips.map((ip) => {
                    const until = ip.recently_blocked
                      ? blockedUntil(ip.last_suspended_at, blockMinutes)
                      : null;
                    return (
                      <tr key={ip.ip}>
                        <td className="wrap">
                          <code>{ip.ip}</code>
                          {ip.ptr && (
                            <>
                              <br />
                              <span className="muted small" title={ip.ptr}>
                                {compact ? ip.domain : ip.ptr}
                              </span>
                            </>
                          )}
                        </td>
                        <td>
                          {ip.trusted ? (
                            <span className="badge ok">
                              {t("loginSecurity.trusted")}
                            </span>
                          ) : ip.suspended > 0 ? (
                            <span
                              className={`badge ${ip.recently_blocked ? "bad" : "warn"}`}
                              title={formatDate(ip.last_suspended_at)}
                            >
                              {ip.recently_blocked
                                ? t("loginSecurity.blockedNow")
                                : t("loginSecurity.blocked", {
                                    count: ip.suspended,
                                  })}
                            </span>
                          ) : (
                            <span className="muted">
                              {t("loginSecurity.notBlocked")}
                            </span>
                          )}
                          {until && until.getTime() > Date.now() && (
                            <>
                              <br />
                              <span className="muted small">
                                {t("loginSecurity.until", {
                                  time: until.toLocaleTimeString([], {
                                    hour: "2-digit",
                                    minute: "2-digit",
                                  }),
                                })}
                              </span>
                            </>
                          )}
                          {ip.last_suspended_at && !compact && !until && (
                            <>
                              <br />
                              <span className="muted small">
                                {formatRelative(ip.last_suspended_at)}
                              </span>
                            </>
                          )}
                        </td>
                        <td className="num">
                          {(
                            ip.failed_mailbox + ip.failed_smtp
                          ).toLocaleString()}
                          {!compact && (
                            <>
                              <br />
                              <span className="muted small">
                                {t("loginSecurity.split", {
                                  mailbox: ip.failed_mailbox,
                                  smtp: ip.failed_smtp,
                                })}
                              </span>
                            </>
                          )}
                        </td>
                        {!compact && (
                          <td className="wrap small">
                            {ip.accounts.join(", ") || "—"}
                          </td>
                        )}
                        <td title={formatDate(ip.last_seen)}>
                          {formatRelative(ip.last_seen)}
                        </td>
                        <td>
                          {ip.pending ? (
                            <span className="badge info">
                              {t(`loginSecurity.pending.${ip.pending}`)}
                            </span>
                          ) : ip.trusted ? (
                            <IpButton
                              ip={ip.ip}
                              action="untrust"
                              label={t("loginSecurity.untrust")}
                              onDone={done(ip.ip, "untrust")}
                            />
                          ) : (
                            (ip.suspended > 0 || !compact) && (
                              <IpButton
                                ip={ip.ip}
                                action="trust"
                                label={
                                  ip.suspended > 0
                                    ? t("loginSecurity.unblock")
                                    : t("loginSecurity.allow")
                                }
                                onDone={done(ip.ip, "trust")}
                              />
                            )
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </LoadView>
  );
}

/** Carbonio's blocking rule, trusted addresses (never blocked) and the latest changes. */
export function TrustedIpsPanel({ refresh = 0 }: { refresh?: number }) {
  const { t } = useTranslation();
  const [load, reload] = useLoad<LoginFilter>(
    () => api.loginFilter(),
    [refresh],
    30000,
  );
  const [ip, setIp] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function add() {
    setBusy(true);
    setError(null);
    try {
      await api.ipAction("trust", ip.trim());
      setIp("");
      reload();
    } catch (err) {
      setError(t("error", { message: errorMessage(err) }));
    } finally {
      setBusy(false);
    }
  }

  return (
    <LoadView load={load} reload={reload}>
      {(d) => {
        const pending = d.actions.filter((a) => a.status === "pending");
        return (
          <div className="stack">
            {d.config ? (
              <p className="muted small">
                {t("loginSecurity.rule", {
                  max: d.config.max_failed ?? "?",
                  minutes: d.config.block_minutes ?? "?",
                })}
              </p>
            ) : (
              <p className="alert warn small">{t("loginSecurity.noConfig")}</p>
            )}
            {d.config && (
              <>
                {d.config.safe_ips.length === 0 ? (
                  <p className="muted">{t("loginSecurity.noTrusted")}</p>
                ) : (
                  <ul className="trusted-list">
                    {d.config.safe_ips.map((s) => {
                      const waiting = pending.find((a) => a.ip === s);
                      return (
                        <li key={s}>
                          <code>{s}</code>
                          {waiting ? (
                            <span className="badge info">
                              {t(`loginSecurity.pending.${waiting.action}`)}
                            </span>
                          ) : (
                            <IpButton
                              ip={s}
                              action="untrust"
                              label={t("loginSecurity.untrust")}
                              onDone={(err) =>
                                err
                                  ? setError(t("error", { message: err }))
                                  : reload()
                              }
                            />
                          )}
                        </li>
                      );
                    })}
                  </ul>
                )}
                <form
                  className="row"
                  onSubmit={(e) => {
                    e.preventDefault();
                    if (ip.trim()) void add();
                  }}
                >
                  <label className="grow">
                    {t("loginSecurity.addTrusted")}
                    <input
                      value={ip}
                      placeholder="203.0.113.9 ou 192.168.8.0/24"
                      onChange={(e) => setIp(e.target.value)}
                    />
                  </label>
                  <button
                    type="submit"
                    className="align-end"
                    disabled={busy || !ip.trim()}
                  >
                    {t("loginSecurity.addButton")}
                  </button>
                </form>
              </>
            )}
            {error && (
              <p role="alert" className="alert error small">
                {error}
              </p>
            )}
            {d.actions.length > 0 && (
              <details>
                <summary>{t("loginSecurity.history")}</summary>
                <ul className="plain small">
                  {d.actions.map((a) => (
                    <li key={a.id}>
                      {formatDate(a.created_at)} ·{" "}
                      {t(`loginSecurity.actions.${a.action}`)}{" "}
                      <code>{a.ip}</code> · <StatusBadge status={a.status} />
                      {a.error && <span className="bad-text"> {a.error}</span>}
                    </li>
                  ))}
                </ul>
              </details>
            )}
            <p className="muted small">{t("loginSecurity.trustHelp")}</p>
          </div>
        );
      }}
    </LoadView>
  );
}

/** Where spam comes from: client IPs grouped by the domain of their reverse DNS name. */
export function SpamSourcesPanel({
  period,
  limit = 100,
  compact = false,
}: {
  period: MailPeriod;
  limit?: number;
  compact?: boolean;
}) {
  const { t } = useTranslation();
  const [load, reload] = useLoad<SpamSources>(
    () => api.spamSources(period),
    [period],
    60000,
  );
  return (
    <LoadView load={load} reload={reload}>
      {(d) =>
        d.sources.length === 0 ? (
          <p className="muted">{t("mail.noSpam")}</p>
        ) : (
          <div className={compact ? "stack" : "split"}>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>{t("spamSources.network")}</th>
                    <th className="num">{t("mail.count")}</th>
                    <th>{t("spamSources.ips")}</th>
                    {!compact && <th>{t("spamSources.senders")}</th>}
                  </tr>
                </thead>
                <tbody>
                  {d.sources.slice(0, limit).map((s) => (
                    <tr key={s.domain ?? "?"}>
                      <td className="wrap">
                        {s.domain ? (
                          <strong>{s.domain}</strong>
                        ) : (
                          <span className="muted">
                            {t("spamSources.noPtr")}
                          </span>
                        )}
                      </td>
                      <td className="num">{s.count.toLocaleString()}</td>
                      <td className="wrap small">
                        {s.ips.slice(0, compact ? 3 : 20).map((ip) => (
                          <span
                            key={ip.ip}
                            className="ip-chip"
                            title={ip.ptr ?? ""}
                          >
                            <code>{ip.ip}</code>
                            {!compact && ip.ptr && (
                              <span className="muted"> {ip.ptr}</span>
                            )}
                          </span>
                        ))}
                        {s.ips.length > (compact ? 3 : 20) && " …"}
                      </td>
                      {!compact && (
                        <td className="wrap small">
                          {s.sender_domains.join(", ") || "—"}
                        </td>
                      )}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {!compact && d.sender_domains.length > 0 && (
              <section>
                <h3>{t("spamSources.senderDomains")}</h3>
                <div className="table-wrap">
                  <table>
                    <tbody>
                      {d.sender_domains.slice(0, 20).map((s) => (
                        <tr key={s.domain}>
                          <td className="wrap">{s.domain}</td>
                          <td className="num">{s.count.toLocaleString()}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </section>
            )}
          </div>
        )
      }
    </LoadView>
  );
}
