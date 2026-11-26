import { useState } from "react";
import type { FormEvent } from "react";
import { useTranslation } from "react-i18next";

import { api } from "../api/endpoints";
import type { AgentTuning, AgentTuningInput } from "../api/types";
import { formatDate } from "../utils/format";
import { errorMessage } from "./common";
import { NoticeBar, type Notice } from "./notices";

function portsText(ports: Record<string, number>): string {
  return Object.entries(ports)
    .map(([name, port]) => `${name}=${port}`)
    .join("\n");
}

function parsePorts(text: string): Record<string, number> {
  const out: Record<string, number> = {};
  for (const line of text.split(/[\n,]+/)) {
    const clean = line.trim();
    if (!clean) continue;
    const [name, port] = clean.split(/[=:\s]+/);
    if (!name || !port || !/^\d+$/.test(port)) throw new Error(clean);
    out[name.toLowerCase()] = Number(port);
  }
  return out;
}

/** Settings → Agent: performance, disk guard and watchers of the Carbonio agent. */
export function AgentSettingsForm({
  initial,
  onSaved,
}: {
  initial: AgentTuning;
  onSaved: () => void;
}) {
  const { t } = useTranslation();
  const [form, setForm] = useState<AgentTuningInput>(initial);
  const [ports, setPorts] = useState(portsText(initial.service_ports));
  const [notice, setNotice] = useState<Notice>(null);
  const set = <K extends keyof AgentTuningInput>(
    k: K,
    v: AgentTuningInput[K],
  ) => setForm((f) => ({ ...f, [k]: v }));
  const num =
    (k: keyof AgentTuningInput) => (e: { target: { value: string } }) =>
      set(k, Number(e.target.value) as never);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setNotice(null);
    try {
      let parsed: Record<string, number>;
      try {
        parsed = parsePorts(ports);
      } catch (err) {
        throw new Error(
          t("agentSettings.badPorts", { line: errorMessage(err) }),
        );
      }
      const {
        max_concurrent_accounts,
        max_bandwidth_mbps,
        max_load_per_cpu,
        min_free_gb_local,
        min_free_percent_local,
        min_free_gb_network,
        service_monitor,
        mail_monitor,
        autostart_enabled,
        autostart_minutes,
        mail_test_after_start,
        mail_test_internal,
        mail_test_external,
        cert_monitor,
        cert_name,
        cert_alert_days,
        cert_renew_enabled,
        cert_renew_days,
      } = form;
      await api.saveAgentTuning({
        max_concurrent_accounts,
        max_bandwidth_mbps,
        max_load_per_cpu,
        min_free_gb_local,
        min_free_percent_local,
        min_free_gb_network,
        service_monitor,
        mail_monitor,
        autostart_enabled,
        autostart_minutes,
        mail_test_after_start,
        mail_test_internal: mail_test_internal.trim(),
        mail_test_external: mail_test_external.trim(),
        cert_monitor,
        cert_name: cert_name.trim().toLowerCase(),
        cert_alert_days,
        cert_renew_enabled,
        cert_renew_days,
        service_ports: parsed,
      });
      setNotice({ kind: "ok", text: t("agentSettings.saved") });
      onSaved();
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
      onSubmit={(e) => void submit(e)}
      aria-label={t("agentSettings.title")}
    >
      <h2>{t("agentSettings.title")}</h2>
      <p className="muted">{t("agentSettings.intro")}</p>
      {initial.revision === 0 && (
        <p className="alert info">{t("agentSettings.fromEnv")}</p>
      )}
      {initial.agents.length > 0 && (
        <ul className="muted small">
          {initial.agents.map((a) => (
            <li key={a.name}>
              {a.applied && a.applied.revision === initial.revision
                ? t("agentSettings.applied", {
                    agent: a.name,
                    date: formatDate(a.applied.at),
                  })
                : t("agentSettings.pending", { agent: a.name })}
            </li>
          ))}
        </ul>
      )}

      <fieldset>
        <legend>{t("agentSettings.performance")}</legend>
        <div className="row">
          <label>
            {t("agentSettings.concurrency")}
            <input
              type="number"
              min={1}
              max={8}
              value={form.max_concurrent_accounts}
              onChange={num("max_concurrent_accounts")}
            />
          </label>
          <label>
            {t("agentSettings.bandwidth")}
            <input
              type="number"
              min={0}
              value={form.max_bandwidth_mbps}
              onChange={num("max_bandwidth_mbps")}
            />
          </label>
          <label>
            {t("agentSettings.load")}
            <input
              type="number"
              min={0}
              step="0.5"
              value={form.max_load_per_cpu}
              onChange={num("max_load_per_cpu")}
            />
          </label>
        </div>
        <p className="muted small">{t("agentSettings.performanceHelp")}</p>
      </fieldset>

      <fieldset>
        <legend>{t("agentSettings.disk")}</legend>
        <div className="row">
          <label>
            {t("agentSettings.freeLocalGb")}
            <input
              type="number"
              min={0}
              value={form.min_free_gb_local}
              onChange={num("min_free_gb_local")}
            />
          </label>
          <label>
            {t("agentSettings.freeLocalPct")}
            <input
              type="number"
              min={0}
              max={90}
              value={form.min_free_percent_local}
              onChange={num("min_free_percent_local")}
            />
          </label>
          <label>
            {t("agentSettings.freeNetworkGb")}
            <input
              type="number"
              min={0}
              value={form.min_free_gb_network}
              onChange={num("min_free_gb_network")}
            />
          </label>
        </div>
        <p className="muted small">{t("agentSettings.diskHelp")}</p>
      </fieldset>

      <fieldset>
        <legend>{t("agentSettings.watchers")}</legend>
        <label className="inline">
          <input
            type="checkbox"
            checked={form.service_monitor}
            onChange={(e) => set("service_monitor", e.target.checked)}
          />
          {t("agentSettings.serviceMonitor")}
        </label>
        <label>
          {t("agentSettings.ports")}
          <textarea
            rows={4}
            value={ports}
            disabled={!form.service_monitor}
            onChange={(e) => setPorts(e.target.value)}
          />
        </label>
        <label className="inline">
          <input
            type="checkbox"
            checked={form.mail_monitor}
            onChange={(e) => set("mail_monitor", e.target.checked)}
          />
          {t("agentSettings.mailMonitor")}
        </label>
      </fieldset>
      <fieldset>
        <legend>{t("agentSettings.autostart")}</legend>
        <label className="inline">
          <input
            type="checkbox"
            checked={form.autostart_enabled}
            onChange={(e) => set("autostart_enabled", e.target.checked)}
          />
          {t("agentSettings.autostartEnabled")}
        </label>
        <div className="row">
          <label>
            {t("agentSettings.autostartMinutes")}
            <input
              type="number"
              min={1}
              max={240}
              value={form.autostart_minutes}
              disabled={!form.autostart_enabled}
              onChange={num("autostart_minutes")}
            />
          </label>
        </div>
        <p className="muted small">{t("agentSettings.autostartHelp")}</p>
        <label className="inline">
          <input
            type="checkbox"
            checked={form.mail_test_after_start}
            onChange={(e) => set("mail_test_after_start", e.target.checked)}
          />
          {t("agentSettings.mailTestAfterStart")}
        </label>
        <div className="row">
          <label className="grow">
            {t("agentSettings.mailTestInternal")}
            <input
              type="email"
              placeholder="utilisateur@lnhc.dz"
              value={form.mail_test_internal}
              onChange={(e) => set("mail_test_internal", e.target.value)}
            />
          </label>
          <label className="grow">
            {t("agentSettings.mailTestExternal")}
            <input
              type="email"
              placeholder="adresse@gmail.com"
              value={form.mail_test_external}
              onChange={(e) => set("mail_test_external", e.target.value)}
            />
          </label>
        </div>
        <p className="muted small">{t("agentSettings.mailTestHelp")}</p>
      </fieldset>
      <fieldset>
        <legend>{t("agentSettings.cert")}</legend>
        <label className="inline">
          <input
            type="checkbox"
            checked={form.cert_monitor}
            onChange={(e) => set("cert_monitor", e.target.checked)}
          />
          {t("agentSettings.certMonitor")}
        </label>
        <div className="row">
          <label className="grow">
            {t("agentSettings.certName")}
            <input
              placeholder="mail.lnhc.dz"
              value={form.cert_name}
              onChange={(e) => set("cert_name", e.target.value)}
            />
          </label>
          <label>
            {t("agentSettings.certAlertDays")}
            <input
              type="number"
              min={1}
              max={90}
              value={form.cert_alert_days}
              onChange={num("cert_alert_days")}
            />
          </label>
        </div>
        <label className="inline">
          <input
            type="checkbox"
            checked={form.cert_renew_enabled}
            onChange={(e) => set("cert_renew_enabled", e.target.checked)}
          />
          {t("agentSettings.certRenew")}
        </label>
        <label>
          {t("agentSettings.certRenewDays")}
          <input
            type="number"
            min={1}
            max={60}
            value={form.cert_renew_days}
            disabled={!form.cert_renew_enabled}
            onChange={num("cert_renew_days")}
          />
        </label>
        <p className="muted small">{t("agentSettings.certHelp")}</p>
      </fieldset>
      <p className="muted small">{t("agentSettings.envOnly")}</p>
      <NoticeBar notice={notice} />
      <div className="actions">
        <button type="submit">{t("save")}</button>
      </div>
    </form>
  );
}
