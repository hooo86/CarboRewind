import { useTranslation } from "react-i18next";

import { api } from "../api/endpoints";
import type { LoginSecurity, MailPeriod, SpamSources } from "../api/types";
import { formatDate, formatRelative } from "../utils/format";
import { LoadView, useLoad } from "./common";

/** IPs with failed logins, and those Carbonio blocked after repeated failures. */
export function BlockedIpsPanel({
  period,
  onlyBlocked = false,
  limit = 100,
  compact = false,
}: {
  period: MailPeriod;
  onlyBlocked?: boolean;
  limit?: number;
  compact?: boolean;
}) {
  const { t } = useTranslation();
  const [load, reload] = useLoad<LoginSecurity>(
    () => api.mailSecurity(period, onlyBlocked, limit),
    [period, onlyBlocked, limit],
    60000,
  );
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
          {!d.reported && (
            <p className="alert warn small">{t("loginSecurity.notReported")}</p>
          )}
          {d.ips.length === 0 ? (
            <p className="muted">{t("loginSecurity.none")}</p>
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
                  </tr>
                </thead>
                <tbody>
                  {d.ips.map((ip) => (
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
                        {ip.suspended > 0 ? (
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
                        {ip.last_suspended_at && !compact && (
                          <>
                            <br />
                            <span className="muted small">
                              {formatRelative(ip.last_suspended_at)}
                            </span>
                          </>
                        )}
                      </td>
                      <td className="num">
                        {(ip.failed_mailbox + ip.failed_smtp).toLocaleString()}
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
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
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
