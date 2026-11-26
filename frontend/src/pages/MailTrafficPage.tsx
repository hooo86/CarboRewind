import { useState } from "react";
import type { FormEvent } from "react";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";

import { api } from "../api/endpoints";
import type { MailLimits, MailOverview, MailPeriod } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import {
  LoadView,
  StatusBadge,
  errorMessage,
  useLoad,
} from "../components/common";
import { NoticeBar, type Notice } from "../components/notices";
import { formatDate, formatRelative } from "../utils/format";

const PERIODS: MailPeriod[] = ["1h", "24h", "7d", "30d"];
const NUMBERS: (keyof MailLimits)[] = [
  "sent_messages_hour",
  "sent_recipients_hour",
  "sent_messages_day",
  "sent_recipients_day",
  "spam_sent_day",
];
const STALE_MS = 10 * 60 * 1000;

function Tile({
  label,
  value,
  tone,
}: {
  label: string;
  value: number;
  tone?: string;
}) {
  return (
    <div className={`tile ${tone ?? ""}`}>
      <span className="tile-value">{value.toLocaleString()}</span>
      <span className="tile-label">{label}</span>
    </div>
  );
}

function Chart({ data }: { data: MailOverview }) {
  const { t } = useTranslation();
  const max = Math.max(
    1,
    ...data.hourly.map((h) => Math.max(h.sent_messages, h.spam_received)),
  );
  const daily = data.period === "7d" || data.period === "30d";
  if (data.hourly.length === 0)
    return <p className="muted">{t("mail.noData")}</p>;
  return (
    <div className="chart" role="img" aria-label={t("mail.chart")}>
      {data.hourly.map((h) => (
        <div
          key={h.at}
          className="chart-col"
          title={`${formatDate(h.at)} — ${t("mail.sent")}: ${h.sent_messages}, ${t("mail.spamIn")}: ${h.spam_received}`}
        >
          <div className="chart-bars">
            <span
              className="bar sent"
              style={{ height: `${(h.sent_messages / max) * 100}%` }}
            />
            <span
              className="bar spam"
              style={{ height: `${(h.spam_received / max) * 100}%` }}
            />
          </div>
          <span className="chart-label">
            {daily
              ? new Date(h.at).toLocaleDateString(undefined, {
                  day: "2-digit",
                  month: "2-digit",
                })
              : new Date(h.at).getHours().toString().padStart(2, "0")}
          </span>
        </div>
      ))}
      <p className="chart-legend">
        <span className="bar sent" /> {t("mail.sent")}{" "}
        <span className="bar spam" /> {t("mail.spamIn")}
      </p>
    </div>
  );
}

export function LimitsForm({
  initial,
  onSaved,
}: {
  initial: MailLimits;
  onSaved: () => void;
}) {
  const { t } = useTranslation();
  const [form, setForm] = useState<MailLimits>(initial);
  const [exempt, setExempt] = useState(initial.exempt.join("\n"));
  const [notice, setNotice] = useState<Notice>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setNotice(null);
    try {
      await api.saveMailLimits({
        ...form,
        exempt: exempt
          .split(/[\s,;]+/)
          .map((v) => v.trim())
          .filter(Boolean),
      });
      setNotice({ kind: "ok", text: t("mail.limitsSaved") });
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
      <h2>{t("mail.limits")}</h2>
      <p className="muted">{t("mail.limitsHelp")}</p>
      <label className="inline">
        <input
          type="checkbox"
          checked={form.enabled}
          onChange={(e) => setForm({ ...form, enabled: e.target.checked })}
        />
        {t("mail.enabled")}
      </label>
      <div className="row">
        {NUMBERS.map((key) => (
          <label key={key}>
            {t(`mail.metric.${key}`)}
            <input
              type="number"
              min={0}
              value={form[key] as number}
              onChange={(e) =>
                setForm({ ...form, [key]: Number(e.target.value) })
              }
            />
          </label>
        ))}
      </div>
      <fieldset>
        <legend>{t("mail.action")}</legend>
        <label className="inline">
          <input
            type="radio"
            name="mail-action"
            checked={form.action === "alert"}
            onChange={() => setForm({ ...form, action: "alert" })}
          />
          {t("mail.actionAlert")}
        </label>
        <label className="inline">
          <input
            type="radio"
            name="mail-action"
            checked={form.action === "lock"}
            onChange={() => setForm({ ...form, action: "lock" })}
          />
          {t("mail.actionLock")}
        </label>
      </fieldset>
      <label>
        {t("mail.exempt")}
        <textarea
          rows={3}
          value={exempt}
          placeholder="newsletter@example.com"
          onChange={(e) => setExempt(e.target.value)}
        />
      </label>
      <NoticeBar notice={notice} />
      <div className="actions">
        <button type="submit">{t("save")}</button>
      </div>
    </form>
  );
}

export function MailTrafficPage() {
  const { t } = useTranslation();
  const { canManageSettings } = useAuth();
  const [period, setPeriod] = useState<MailPeriod>("24h");
  const [load, reload] = useLoad<MailOverview>(
    () => api.mailOverview(period),
    [period],
    60000,
  );
  const [notice, setNotice] = useState<Notice>(null);

  if (!canManageSettings) {
    return (
      <p role="alert" className="alert error">
        {t("settings.forbidden")}
      </p>
    );
  }

  async function act(run: () => Promise<string>) {
    setNotice(null);
    try {
      setNotice({ kind: "ok", text: await run() });
      reload();
    } catch (err) {
      setNotice({
        kind: "error",
        text: t("error", { message: errorMessage(err) }),
      });
    }
  }

  const lock = (
    accountId: string,
    email: string,
    action: "lock" | "unlock",
  ) => {
    if (!window.confirm(t(`mail.confirm.${action}`, { email }))) return;
    void act(async () => {
      const r = await api.accountLock(accountId, action);
      return t(r.queued ? `mail.queued.${action}` : "mail.alreadyQueued", {
        email,
      });
    });
  };

  return (
    <>
      <div className="toolbar">
        <h1 className="grow">{t("mail.title")}</h1>
        <div className="actions" role="group" aria-label={t("mail.period")}>
          {PERIODS.map((p) => (
            <button
              key={p}
              type="button"
              className={`small ${p === period ? "" : "secondary"}`}
              aria-pressed={p === period}
              onClick={() => setPeriod(p)}
            >
              {t(`mail.periods.${p}`)}
            </button>
          ))}
        </div>
        <Link className="button secondary" to="/blocking?tab=limits">
          {t("mail.limits")}
        </Link>
      </div>
      <p className="muted">{t("mail.intro")}</p>
      <NoticeBar notice={notice} />
      <LoadView load={load} reload={reload}>
        {(d) => {
          const openAlerts = d.alerts.filter((a) => !a.acknowledged_at);
          const lastReport = d.agents
            .map((a) => a.reported_at)
            .filter(Boolean)
            .sort()
            .at(-1);
          return (
            <>
              {!lastReport ? (
                <p className="alert warn">{t("mail.noAgent")}</p>
              ) : (
                Date.now() - new Date(lastReport).getTime() > STALE_MS && (
                  <p className="alert warn">
                    {t("mail.stale", { when: formatRelative(lastReport) })}
                  </p>
                )
              )}
              {!d.limits.enabled && (
                <p className="alert info">{t("mail.limitsOff")}</p>
              )}
              <section className="tiles" aria-label={t("mail.totals")}>
                <Tile label={t("mail.sent")} value={d.totals.sent_messages} />
                <Tile
                  label={t("mail.recipients")}
                  value={d.totals.sent_recipients}
                />
                <Tile
                  label={t("mail.spamIn")}
                  value={d.totals.spam_received}
                  tone={d.totals.spam_received ? "warn" : undefined}
                />
                <Tile
                  label={t("mail.spamOut")}
                  value={d.totals.spam_sent}
                  tone={d.totals.spam_sent ? "bad" : undefined}
                />
                <Tile
                  label={t("mail.spamSenders")}
                  value={d.totals.spam_senders}
                />
                <Tile
                  label={t("mail.openAlerts")}
                  value={openAlerts.length}
                  tone={openAlerts.length ? "bad" : "ok"}
                />
              </section>
              <Chart data={d} />

              <h2>{t("mail.alerts")}</h2>
              {d.pending_actions.length > 0 && (
                <p className="alert info">
                  {t("mail.pending", {
                    list: d.pending_actions
                      .map((p) => `${p.email} (${t(`mail.do.${p.action}`)})`)
                      .join(", "),
                  })}
                </p>
              )}
              {d.alerts.length === 0 ? (
                <p className="muted">{t("mail.noAlert")}</p>
              ) : (
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>{t("mail.when")}</th>
                        <th>{t("mail.account")}</th>
                        <th>{t("mail.measure")}</th>
                        <th className="num">{t("mail.value")}</th>
                        <th>{t("mail.actionTaken")}</th>
                        <th>{t("mail.accountState")}</th>
                        <th />
                      </tr>
                    </thead>
                    <tbody>
                      {d.alerts.map((a) => (
                        <tr
                          key={a.id}
                          className={a.acknowledged_at ? "muted" : undefined}
                        >
                          <td>{formatDate(a.created_at)}</td>
                          <td>{a.email}</td>
                          <td>{t(`mail.metric.${a.metric}`)}</td>
                          <td className="num">
                            {a.value.toLocaleString()} /{" "}
                            {a.threshold.toLocaleString()}
                          </td>
                          <td>{t(`mail.do.${a.action}`)}</td>
                          <td>
                            <StatusBadge status={a.account_status} />
                          </td>
                          <td className="actions">
                            {!a.acknowledged_at && (
                              <button
                                type="button"
                                className="small secondary"
                                onClick={() =>
                                  void act(async () => {
                                    await api.ackMailAlert(a.id);
                                    return t("mail.acknowledged");
                                  })
                                }
                              >
                                {t("mail.ack")}
                              </button>
                            )}
                            {a.account_id && a.account_status === "locked" && (
                              <button
                                type="button"
                                className="small"
                                onClick={() =>
                                  lock(a.account_id ?? "", a.email, "unlock")
                                }
                              >
                                {t("mail.unlock")}
                              </button>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}

              <h2>{t("mail.topSenders")}</h2>
              {d.top_senders.length === 0 ? (
                <p className="muted">{t("mail.noData")}</p>
              ) : (
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>{t("mail.account")}</th>
                        <th className="num">{t("mail.sent")}</th>
                        <th className="num">{t("mail.recipients")}</th>
                        <th className="num">{t("mail.spamOut")}</th>
                        <th>{t("mail.accountState")}</th>
                        <th />
                      </tr>
                    </thead>
                    <tbody>
                      {d.top_senders.map((s) => (
                        <tr key={s.email}>
                          <td>
                            {s.account_id ? (
                              <Link to={`/accounts/${s.account_id}`}>
                                {s.email}
                              </Link>
                            ) : (
                              s.email
                            )}
                          </td>
                          <td className="num">{s.messages.toLocaleString()}</td>
                          <td className="num">
                            {s.recipients.toLocaleString()}
                          </td>
                          <td
                            className={`num ${s.spam_sent ? "bad-text" : ""}`}
                          >
                            {s.spam_sent.toLocaleString()}
                          </td>
                          <td>
                            <StatusBadge status={s.status} />
                          </td>
                          <td>
                            {s.account_id && (
                              <button
                                type="button"
                                className="small secondary"
                                onClick={() =>
                                  lock(
                                    s.account_id ?? "",
                                    s.email,
                                    s.status === "locked" ? "unlock" : "lock",
                                  )
                                }
                              >
                                {s.status === "locked"
                                  ? t("mail.unlock")
                                  : t("mail.lock")}
                              </button>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}

              <div className="split">
                <section>
                  <h2>{t("mail.spamRecipients")}</h2>
                  {d.spam_recipients.length === 0 ? (
                    <p className="muted">{t("mail.noSpam")}</p>
                  ) : (
                    <div className="table-wrap">
                      <table>
                        <tbody>
                          {d.spam_recipients.map((r) => (
                            <tr key={r.email}>
                              <td>{r.email}</td>
                              <td className="num">
                                {r.count.toLocaleString()}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                </section>
                <section>
                  <h2>{t("mail.spamSendersTitle")}</h2>
                  {d.spam_senders.length === 0 ? (
                    <p className="muted">{t("mail.noSpam")}</p>
                  ) : (
                    <div className="table-wrap">
                      <table>
                        <thead>
                          <tr>
                            <th>{t("mail.sender")}</th>
                            <th className="num">{t("mail.count")}</th>
                            <th>{t("mail.ips")}</th>
                            <th>{t("mail.last")}</th>
                          </tr>
                        </thead>
                        <tbody>
                          {d.spam_senders.map((s) => (
                            <tr key={s.sender}>
                              <td className="wrap">{s.sender}</td>
                              <td className="num">
                                {s.count.toLocaleString()}
                              </td>
                              <td className="wrap">
                                <code>{s.ips.join(", ") || "—"}</code>
                              </td>
                              <td>{formatRelative(s.last)}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                </section>
              </div>
            </>
          );
        }}
      </LoadView>
      <section className="card stack" aria-labelledby="blocking-link-title">
        <h2 id="blocking-link-title">{t("blocking.title")}</h2>
        <p className="muted small">{t("blocking.moved")}</p>
        <div className="actions wrap-actions">
          <Link className="button secondary" to="/blocking">
            {t("blocking.tabs.senders")}
          </Link>
          <Link className="button secondary" to="/blocking?tab=spam">
            {t("blocking.tabs.spam")}
          </Link>
          <Link className="button secondary" to="/blocking?tab=ips">
            {t("blocking.tabs.ips")}
          </Link>
        </div>
      </section>
    </>
  );
}
