import { useState } from "react";
import { useTranslation } from "react-i18next";

import { api } from "../api/endpoints";
import type { ServicesView } from "../api/types";
import { formatDate, formatRelative } from "../utils/format";
import { errorMessage } from "./common";
import { NoticeBar, type Notice } from "./notices";

function tone(days: number | undefined, alert: number): string | undefined {
  if (days === undefined) return undefined;
  if (days <= 0 || days <= alert / 2) return "bad-text";
  if (days <= alert) return "warn-text";
  return undefined;
}

/** Supervision: TLS certificate of the mail server (expiry, renewal), for one agent. */
export function CertificatePanel({
  view,
  agentId,
  onChange,
}: {
  view: ServicesView;
  agentId: string | null;
  onChange: () => void;
}) {
  const { t } = useTranslation();
  const [notice, setNotice] = useState<Notice>(null);
  const [busy, setBusy] = useState(false);
  const cert = view.certificate;
  if (!cert) return null;
  const result = cert.last_result;
  const asked = view.cert_renew_request;

  async function renew() {
    if (!agentId) return;
    setBusy(true);
    setNotice(null);
    try {
      await api.certRenew(agentId);
      setNotice({ kind: "ok", text: t("monitoring.cert.renewAsked") });
      onChange();
    } catch (err) {
      setNotice({
        kind: "error",
        text: t("error", { message: errorMessage(err) }),
      });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="stack" aria-label={t("monitoring.cert.title")}>
      <div className="toolbar">
        <h3 className="grow">
          {t("monitoring.cert.title")} — {cert.name || "?"}
        </h3>
        {agentId && cert.name && (
          <button
            type="button"
            className="small secondary"
            disabled={busy}
            onClick={() => void renew()}
          >
            {t("monitoring.cert.renew")}
          </button>
        )}
      </div>
      <p className={tone(cert.days_left ?? undefined, cert.alert_days)}>
        {cert.days_left === null
          ? t("monitoring.cert.unknown")
          : cert.days_left <= 0
            ? t("monitoring.cert.expired")
            : t("monitoring.cert.daysLeft", {
                count: Math.floor(cert.days_left),
              })}{" "}
        <span className="muted small">
          {t("monitoring.cert.checked", {
            when: formatRelative(cert.checked_at),
          })}
        </span>
      </p>
      <table>
        <thead>
          <tr>
            <th>{t("monitoring.cert.port")}</th>
            <th>{t("monitoring.cert.names")}</th>
            <th>{t("monitoring.cert.issuer")}</th>
            <th>{t("monitoring.cert.expires")}</th>
          </tr>
        </thead>
        <tbody>
          {cert.certificates.map((c) => (
            <tr key={c.port}>
              <td>
                {c.service} {c.port}
              </td>
              {c.error ? (
                <td colSpan={3} className="muted small">
                  {c.error}
                </td>
              ) : (
                <>
                  <td className="small">
                    {(c.names ?? []).join(", ")}
                    {c.matches === false && (
                      <span className="bad-text">
                        {" "}
                        {t("monitoring.cert.mismatch")}
                      </span>
                    )}
                  </td>
                  <td className="small">
                    {c.self_signed
                      ? t("monitoring.cert.selfSigned")
                      : (c.issuer ?? "—")}
                  </td>
                  <td className={tone(c.days_left, cert.alert_days)}>
                    {formatDate(c.not_after)}
                  </td>
                </>
              )}
            </tr>
          ))}
        </tbody>
      </table>
      <p className="muted small">
        {cert.renew_enabled
          ? t("monitoring.cert.autoOn", { days: cert.renew_days })
          : t("monitoring.cert.autoOff")}{" "}
        {t("monitoring.cert.alertEvery", { days: cert.alert_days })}
      </p>
      {asked && !result && (
        <p className="muted small">
          {t("monitoring.cert.pending", {
            by: asked.by,
            when: formatDate(asked.at),
          })}
        </p>
      )}
      {result && (
        <p
          className={
            result.renewed ? "ok-text" : result.ok ? "muted" : "bad-text"
          }
        >
          {result.renewed
            ? t("monitoring.cert.renewed", {
                when: formatDate(result.at),
                until: formatDate(result.not_after),
              })
            : result.ok
              ? t("monitoring.cert.notDue", { when: formatDate(result.at) })
              : t("monitoring.cert.failed", {
                  when: formatDate(result.at),
                  error: result.error ?? "?",
                })}
        </p>
      )}
      {cert.note && <p className="muted small">{cert.note}</p>}
      <NoticeBar notice={notice} />
    </div>
  );
}
