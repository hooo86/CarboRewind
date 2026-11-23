import { useState } from "react";
import type { FormEvent } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { api } from "../api/endpoints";
import type { BlacklistResult, BlacklistStatus } from "../api/types";
import { formatDate } from "../utils/format";
import { errorMessage, LoadView, useLoad } from "./common";
import { NoticeBar, type Notice } from "./notices";

function lines(text: string): string[] {
  return text
    .split(/[\s,;]+/)
    .map((x) => x.trim())
    .filter(Boolean);
}

/** One row per address: a badge per list (listed / clean / unknown). */
export function BlacklistTable({ results }: { results: BlacklistResult[] }) {
  const { t } = useTranslation();
  const ips = [...new Set(results.map((r) => r.ip))];
  const zones = [...new Set(results.map((r) => r.zone))];
  if (results.length === 0)
    return <p className="muted">{t("blacklist.noResult")}</p>;
  return (
    <div className="table-wrap">
      <table aria-label={t("blacklist.results")}>
        <thead>
          <tr>
            <th>{t("blacklist.address")}</th>
            {zones.map((z) => (
              <th key={z}>{z}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {ips.map((ip) => (
            <tr key={ip}>
              <td>
                <code>{ip}</code>
              </td>
              {zones.map((zone) => {
                const r = results.find((x) => x.ip === ip && x.zone === zone);
                if (!r) return <td key={zone}>—</td>;
                const tone = r.listed
                  ? "bad"
                  : r.listed === false
                    ? "ok"
                    : "muted";
                const label = r.listed
                  ? t("blacklist.listed")
                  : r.listed === false
                    ? t("blacklist.clean")
                    : t("blacklist.unknown");
                return (
                  <td key={zone}>
                    <span
                      className={`badge ${tone}`}
                      title={
                        r.error ??
                        (r.since
                          ? t("blacklist.since", { date: formatDate(r.since) })
                          : undefined)
                      }
                    >
                      {label}
                    </span>
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Supervision: current state, check now; nothing at all when not set up. */
export function BlacklistPanel() {
  const { t } = useTranslation();
  const [load, reload] = useLoad(() => api.blacklist(), []);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<Notice>(null);
  if (load.state !== "ready") return null;
  const d = load.data;

  async function check() {
    setBusy(true);
    setNotice(null);
    try {
      await api.checkBlacklist();
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

  const listed = d.results.filter((r) => r.listed);
  return (
    <section className="card stack" aria-labelledby="blacklist-title">
      <div className="toolbar">
        <h2 id="blacklist-title" className="grow">
          {t("blacklist.title")}
        </h2>
        {d.settings.ips.length > 0 && (
          <button
            type="button"
            className="secondary small"
            disabled={busy}
            onClick={() => void check()}
          >
            {busy ? t("blacklist.checking") : t("blacklist.checkNow")}
          </button>
        )}
        <Link className="button secondary small" to="/blocking?tab=dnsbl">
          {t("blacklist.settings")}
        </Link>
      </div>
      {d.settings.ips.length === 0 ? (
        <p className="muted">{t("blacklist.notConfigured")}</p>
      ) : (
        <>
          {listed.length > 0 && (
            <p role="alert" className="alert error">
              {t("blacklist.listedAlert", {
                list: listed.map((r) => `${r.ip} (${r.zone})`).join(", "),
              })}
            </p>
          )}
          <p className="muted small">
            {d.checked_at
              ? t("blacklist.checkedAt", { date: formatDate(d.checked_at) })
              : t("blacklist.neverChecked")}
            {!d.settings.enabled && ` — ${t("blacklist.disabled")}`}
          </p>
          <BlacklistTable results={d.results} />
        </>
      )}
      <NoticeBar notice={notice} />
    </section>
  );
}

/** Settings → Blacklists. */
export function BlacklistSettingsForm({
  initial,
  onSaved,
}: {
  initial: BlacklistStatus;
  onSaved: () => void;
}) {
  const { t } = useTranslation();
  const [enabled, setEnabled] = useState(initial.settings.enabled);
  const [ips, setIps] = useState(initial.settings.ips.join("\n"));
  const [zones, setZones] = useState(initial.settings.zones.join("\n"));
  const [interval, setInterval_] = useState(initial.settings.interval_minutes);
  const [notice, setNotice] = useState<Notice>(null);
  const [status, setStatus] = useState(initial);

  async function save(event: FormEvent) {
    event.preventDefault();
    setNotice(null);
    try {
      const saved = await api.saveBlacklist({
        enabled,
        ips: lines(ips),
        zones: lines(zones),
        interval_minutes: interval,
      });
      setStatus(saved);
      setNotice({ kind: "ok", text: t("blacklist.saved") });
      onSaved();
    } catch (err) {
      setNotice({
        kind: "error",
        text: t("error", { message: errorMessage(err) }),
      });
    }
  }

  async function check() {
    setNotice(null);
    try {
      setStatus(await api.checkBlacklist());
    } catch (err) {
      setNotice({
        kind: "error",
        text: t("error", { message: errorMessage(err) }),
      });
    }
  }

  return (
    <form
      className="card stack"
      onSubmit={(e) => void save(e)}
      aria-label={t("blacklist.title")}
    >
      <h2>{t("blacklist.title")}</h2>
      <p className="muted">{t("blacklist.intro")}</p>
      <label className="inline">
        <input
          type="checkbox"
          checked={enabled}
          onChange={(e) => setEnabled(e.target.checked)}
        />
        {t("blacklist.enabled")}
      </label>
      <div className="row">
        <label className="grow">
          {t("blacklist.ips")}
          <textarea
            rows={4}
            value={ips}
            placeholder="203.0.113.10"
            onChange={(e) => setIps(e.target.value)}
          />
        </label>
        <label className="grow">
          {t("blacklist.zones")}
          <textarea
            rows={6}
            value={zones}
            onChange={(e) => setZones(e.target.value)}
          />
        </label>
        <label>
          {t("blacklist.interval")}
          <input
            type="number"
            min={15}
            max={1440}
            value={interval}
            onChange={(e) => setInterval_(Number(e.target.value))}
          />
        </label>
      </div>
      <p className="muted small">{t("blacklist.help")}</p>
      <NoticeBar notice={notice} />
      <div className="actions">
        <button type="submit">{t("save")}</button>
        <button
          type="button"
          className="secondary"
          onClick={() => void check()}
        >
          {t("blacklist.checkNow")}
        </button>
      </div>
      <BlacklistTable results={status.results} />
    </form>
  );
}

export function BlacklistSettingsTab() {
  const [load, reload] = useLoad(() => api.blacklist(), []);
  return (
    <LoadView load={load} reload={reload}>
      {(d) => <BlacklistSettingsForm initial={d} onSaved={reload} />}
    </LoadView>
  );
}
