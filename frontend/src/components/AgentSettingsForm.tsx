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
      <p className="muted small">{t("agentSettings.envOnly")}</p>
      <NoticeBar notice={notice} />
      <div className="actions">
        <button type="submit">{t("save")}</button>
      </div>
    </form>
  );
}
