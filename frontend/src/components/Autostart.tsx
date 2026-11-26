import { useState } from "react";
import { useTranslation } from "react-i18next";

import { api } from "../api/endpoints";
import type { ServicesView } from "../api/types";
import { formatDate } from "../utils/format";
import { errorMessage } from "./common";
import { NoticeBar, type Notice } from "./notices";

const TONE: Record<string, string> = {
  sent: "ok",
  pending: "muted",
  deferred: "warn",
  bounced: "bad",
  expired: "bad",
  error: "bad",
  timeout: "bad",
};

/** Supervision: automatic start of Carbonio and the sending test, for one agent. */
export function AutostartPanel({
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
  const auto = view.autostart;
  const test = view.mail_test;
  if (!auto && !test) return null;
  const paused =
    auto?.paused_until && new Date(auto.paused_until).getTime() > Date.now();

  async function run(action: () => Promise<unknown>, ok?: string) {
    setBusy(true);
    setNotice(null);
    try {
      await action();
      if (ok) setNotice({ kind: "ok", text: ok });
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
    <div className="stack" aria-label={t("monitoring.autostart.title")}>
      <div className="toolbar">
        <h3 className="grow">{t("monitoring.autostart.title")}</h3>
        {agentId && (
          <button
            type="button"
            className="secondary small"
            disabled={busy}
            onClick={() =>
              void run(
                () => api.mailTest(agentId),
                t("monitoring.autostart.testAsked"),
              )
            }
          >
            {t("monitoring.autostart.testNow")}
          </button>
        )}
        {auto?.enabled && (
          <button
            type="button"
            className="secondary small"
            disabled={busy}
            onClick={() => void run(() => api.pauseAutostart(paused ? 0 : 120))}
          >
            {paused
              ? t("monitoring.autostart.resume")
              : t("monitoring.autostart.pause")}
          </button>
        )}
      </div>
      {auto && (
        <p className="muted small">
          {auto.enabled
            ? t("monitoring.autostart.on", { minutes: auto.minutes })
            : t("monitoring.autostart.off")}
          {paused &&
            ` ${t("monitoring.autostart.paused", {
              date: formatDate(auto.paused_until),
            })}`}
          {auto.down_since &&
            ` ${t("monitoring.autostart.downSince", {
              date: formatDate(auto.down_since),
            })}`}
          {auto.down_since &&
            auto.note &&
            ` (${t(`monitoring.autostart.note.${auto.note}`, {
              defaultValue: auto.note,
            })})`}
        </p>
      )}
      {auto?.last_request && (
        <p className="small">
          {t("monitoring.autostart.lastStart", {
            date: formatDate(auto.last_request.at),
          })}
          {" — "}
          {auto.last_result ? (
            <span className={auto.last_result.ok ? "ok-text" : "bad-text"}>
              {auto.last_result.ok
                ? t("monitoring.autostart.startOk")
                : `${t("monitoring.autostart.startFailed")} : ${
                    auto.last_result.error ?? auto.last_result.output ?? ""
                  }`}
            </span>
          ) : (
            t("monitoring.autostart.note.start asked")
          )}
        </p>
      )}
      {test && (
        <div className="stack">
          <p className="small">
            {t("monitoring.autostart.testTitle", {
              date: formatDate(test.started),
              reason: t(`monitoring.autostart.reason.${test.reason}`),
            })}
            {test.done && (
              <>
                {" — "}
                <strong className={test.ok ? "ok-text" : "bad-text"}>
                  {test.ok
                    ? t("monitoring.autostart.testOk")
                    : t("monitoring.autostart.testFailed")}
                </strong>
              </>
            )}
          </p>
          {test.error && <p className="bad-text small">{test.error}</p>}
          <ul className="services">
            {Object.entries(test.results).map(([kind, r]) => (
              <li key={kind}>
                <span className={`badge ${TONE[r.status] ?? "muted"}`}>
                  {t(`monitoring.autostart.kind.${kind}`, {
                    defaultValue: kind,
                  })}{" "}
                  {r.address} —{" "}
                  {t(`monitoring.autostart.status.${r.status}`, {
                    defaultValue: r.status,
                  })}
                </span>
                {r.detail && r.status !== "sent" && (
                  <span className="muted small"> {r.detail}</span>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
      <NoticeBar notice={notice} />
    </div>
  );
}
