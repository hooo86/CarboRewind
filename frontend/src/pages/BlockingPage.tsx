import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useTranslation } from "react-i18next";

import { api } from "../api/endpoints";
import type { AccountItem, MailPeriod, Page } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { BlacklistSettingsTab } from "../components/Blacklist";
import { BlocklistPanel } from "../components/Blocklist";
import { LoadView, errorMessage, useLoad } from "../components/common";
import { NoticeBar, type Notice } from "../components/notices";
import {
  BlockedIpsPanel,
  SpamSourcesPanel,
  TrustedIpsPanel,
} from "../components/SecurityPanels";
import { formatDate } from "../utils/format";
import { LimitsForm } from "./MailTrafficPage";

const TABS = ["senders", "spam", "ips", "accounts", "limits", "dnsbl"] as const;
type Tab = (typeof TABS)[number];
const PERIODS: MailPeriod[] = ["1h", "24h", "7d", "30d"];

function PeriodPicker({
  period,
  onChange,
}: {
  period: MailPeriod;
  onChange: (p: MailPeriod) => void;
}) {
  const { t } = useTranslation();
  return (
    <div className="actions" role="group" aria-label={t("mail.period")}>
      {PERIODS.map((p) => (
        <button
          key={p}
          type="button"
          className={`small ${p === period ? "" : "secondary"}`}
          aria-pressed={p === period}
          onClick={() => onChange(p)}
        >
          {t(`mail.periods.${p}`)}
        </button>
      ))}
    </div>
  );
}

/** Everything that blocks something, in one place: senders, spam origin, IPs, accounts,
 * sending limits, public blacklists (DNSBL). */
export function BlockingPage() {
  const { t } = useTranslation();
  const { canManageSettings } = useAuth();
  const [params, setParams] = useSearchParams();
  const [period, setPeriod] = useState<MailPeriod>("24h");
  const [onlyBlocked, setOnlyBlocked] = useState(false);
  const [tick, setTick] = useState(0);
  if (!canManageSettings) {
    return (
      <p role="alert" className="alert error">
        {t("settings.forbidden")}
      </p>
    );
  }
  const tab: Tab = TABS.includes(params.get("tab") as Tab)
    ? (params.get("tab") as Tab)
    : "senders";
  return (
    <>
      <h1>{t("blocking.title")}</h1>
      <p className="muted">{t("blocking.intro")}</p>
      <div className="tabs" role="tablist" aria-label={t("blocking.title")}>
        {TABS.map((name) => (
          <button
            key={name}
            type="button"
            role="tab"
            aria-selected={tab === name}
            className={tab === name ? "" : "secondary"}
            onClick={() => setParams(name === "senders" ? {} : { tab: name })}
          >
            {t(`blocking.tabs.${name}`)}
          </button>
        ))}
      </div>

      {tab === "senders" && (
        <section className="card stack" aria-labelledby="blocklist-title">
          <h2 id="blocklist-title">{t("blocklist.title")}</h2>
          <BlocklistPanel refresh={tick} />
        </section>
      )}

      {tab === "spam" && (
        <section className="card stack" aria-labelledby="spam-sources-title">
          <div className="toolbar">
            <h2 id="spam-sources-title" className="grow">
              {t("spamSources.title")}
            </h2>
            <PeriodPicker period={period} onChange={setPeriod} />
          </div>
          <p className="muted small">{t("spamSources.intro")}</p>
          <SpamSourcesPanel
            period={period}
            onBlocked={() => setTick((n) => n + 1)}
          />
        </section>
      )}

      {tab === "ips" && (
        <>
          <section className="card stack" aria-labelledby="security-title">
            <div className="toolbar">
              <h2 id="security-title" className="grow">
                {t("loginSecurity.title")}
              </h2>
              <PeriodPicker period={period} onChange={setPeriod} />
            </div>
            <p className="muted small">{t("loginSecurity.intro")}</p>
            <label className="check">
              <input
                type="checkbox"
                checked={onlyBlocked}
                onChange={(e) => setOnlyBlocked(e.target.checked)}
              />
              {t("widgets.onlyBlocked")}
            </label>
            <BlockedIpsPanel
              period={period}
              onlyBlocked={onlyBlocked}
              onChange={() => setTick((n) => n + 1)}
            />
          </section>
          <section className="card stack" aria-labelledby="trusted-title">
            <h2 id="trusted-title">{t("loginSecurity.trustedTitle")}</h2>
            <TrustedIpsPanel refresh={tick} />
          </section>
        </>
      )}

      {tab === "accounts" && <LockedAccounts />}

      {tab === "limits" && (
        <section className="card stack" aria-labelledby="limits-title">
          <h2 id="limits-title">{t("settings.tabs.mail")}</h2>
          <LimitsTab />
        </section>
      )}

      {tab === "dnsbl" && <BlacklistSettingsTab />}
    </>
  );
}

function LimitsTab() {
  const [load, reload] = useLoad(() => api.mailLimits(), []);
  return (
    <LoadView load={load} reload={reload}>
      {(limits) => <LimitsForm initial={limits} onSaved={reload} />}
    </LoadView>
  );
}

/** Accounts locked in Carbonio (by an administrator, a sending limit or failed logins). */
function LockedAccounts() {
  const { t } = useTranslation();
  const [load, reload] = useLoad<Page<AccountItem>>(
    () =>
      api.accountList({
        filters: ["status:in:locked"],
        sort: "email",
        order: "asc",
        limit: 200,
        offset: 0,
      }),
    [],
  );
  const [notice, setNotice] = useState<Notice>(null);
  const [busy, setBusy] = useState(false);

  async function unlock(a: AccountItem) {
    setBusy(true);
    setNotice(null);
    try {
      const r = await api.accountLock(a.id, "unlock");
      setNotice({
        kind: "ok",
        text: r.queued
          ? t("blocking.unlockQueued", { email: a.email })
          : t("mailbox.alreadyPending"),
      });
      reload();
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
    <section className="card stack" aria-labelledby="locked-title">
      <h2 id="locked-title">{t("blocking.lockedTitle")}</h2>
      <p className="muted small">{t("blocking.lockedHelp")}</p>
      <NoticeBar notice={notice} />
      <LoadView load={load} reload={reload}>
        {(page) =>
          page.items.length === 0 ? (
            <p className="muted">{t("blocking.noLocked")}</p>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>{t("blocklist.account")}</th>
                    <th>{t("blocking.lastLogin")}</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {page.items.map((a) => (
                    <tr key={a.id}>
                      <td>
                        <Link to={`/accounts/${a.id}`}>{a.email}</Link>
                        {a.display_name && (
                          <span className="muted small"> {a.display_name}</span>
                        )}
                      </td>
                      <td>{formatDate(a.last_login_at)}</td>
                      <td>
                        <button
                          type="button"
                          className="small secondary"
                          disabled={busy}
                          onClick={() => void unlock(a)}
                        >
                          {t("mail.unlock")}
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )
        }
      </LoadView>
    </section>
  );
}
