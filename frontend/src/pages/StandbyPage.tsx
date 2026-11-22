import { useState } from "react";
import type { FormEvent } from "react";
import { useTranslation } from "react-i18next";

import { api } from "../api/endpoints";
import type { StandbyAgent, StandbyOverview } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { LoadView, errorMessage, useLoad } from "../components/common";
import { NoticeBar, type Notice } from "../components/notices";
import { formatDate, formatRelative } from "../utils/format";

function useLagText(): (seconds: number) => string {
  const { t } = useTranslation();
  return (seconds) => {
    if (seconds < 60) return t("standby.lagNone");
    const minutes = Math.round(seconds / 60);
    return minutes < 120
      ? t("standby.lagMinutes", { count: minutes })
      : t("standby.lagHours", { count: Math.round(minutes / 60) });
  };
}

/** Standby server: a clone of the Carbonio VM kept in line for a disaster. */
export function StandbyPage() {
  const { t } = useTranslation();
  const { canManageUsers } = useAuth();
  const [data, reload] = useLoad<StandbyOverview>(
    () => api.standby(),
    [],
    30_000,
  );
  const [notice, setNotice] = useState<Notice>(null);
  const [busy, setBusy] = useState(false);

  async function run(action: () => Promise<string>) {
    setBusy(true);
    setNotice(null);
    try {
      setNotice({ kind: "ok", text: await action() });
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

  if (!canManageUsers) return <p className="muted">{t("standby.forbidden")}</p>;
  return (
    <>
      <h1>{t("standby.title")}</h1>
      <p className="muted">{t("standby.intro")}</p>
      <NoticeBar notice={notice} />
      <LoadView load={data} reload={reload}>
        {(view) => <Content view={view} busy={busy} run={(a) => void run(a)} />}
      </LoadView>
    </>
  );
}

function Content({
  view,
  busy,
  run,
}: {
  view: StandbyOverview;
  busy: boolean;
  run: (action: () => Promise<string>) => void;
}) {
  const { t } = useTranslation();
  const lagText = useLagText();
  const [enabled, setEnabled] = useState(view.config.enabled);
  const [minutes, setMinutes] = useState(view.config.interval_minutes);
  const standbys = view.agents.filter((a) => a.role === "standby");
  const active = standbys.find((a) => a.status === "active");

  const save = (event: FormEvent) => {
    event.preventDefault();
    run(async () => {
      await api.saveStandby(enabled, minutes);
      return t("standby.saved");
    });
  };

  return (
    <>
      <section className="card stack" aria-labelledby="standby-state">
        <h2 id="standby-state">{t("standby.state")}</h2>
        {!active ? (
          <p className="warn-text">{t("standby.noAgent")}</p>
        ) : !view.config.enabled ? (
          <p className="warn-text">{t("standby.disabled")}</p>
        ) : null}
        <dl className="facts">
          <div>
            <dt>{t("standby.lag")}</dt>
            <dd>{lagText(view.lag_seconds)}</dd>
          </div>
          <div>
            <dt>{t("standby.synced")}</dt>
            <dd>
              {t("standby.syncedValue", {
                synced: view.synced,
                total: view.accounts,
              })}
            </dd>
          </div>
          <div>
            <dt>{t("standby.behind")}</dt>
            <dd>{view.behind}</dd>
          </div>
          <div>
            <dt>{t("standby.failed")}</dt>
            <dd className={view.failed ? "bad-text" : undefined}>
              {view.failed}
            </dd>
          </div>
          <div>
            <dt>{t("standby.pending")}</dt>
            <dd>{view.pending_jobs}</dd>
          </div>
          <div>
            <dt>{t("standby.lastApplied")}</dt>
            <dd title={formatDate(view.last_applied_at)}>
              {formatRelative(view.last_applied_at)}
            </dd>
          </div>
        </dl>
        <div className="actions">
          <button
            type="button"
            className="secondary"
            disabled={busy || !view.config.enabled}
            onClick={() =>
              run(async () => {
                const r = await api.standbySyncNow();
                return t("standby.syncQueued", { count: r.queued });
              })
            }
          >
            {t("standby.syncNow")}
          </button>
        </div>
        {view.problems.length > 0 && (
          <details>
            <summary className="bad-text">
              {t("standby.problems", { count: view.problems.length })}
            </summary>
            <table>
              <thead>
                <tr>
                  <th>{t("standby.account")}</th>
                  <th>{t("standby.status")}</th>
                  <th>{t("standby.error")}</th>
                  <th>{t("standby.when")}</th>
                </tr>
              </thead>
              <tbody>
                {view.problems.map((p) => (
                  <tr key={p.email}>
                    <td>{p.email}</td>
                    <td>{t(`standby.statusValue.${p.status}`)}</td>
                    <td className="small">{p.error ?? "—"}</td>
                    <td>{formatDate(p.applied_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </details>
        )}
      </section>

      <section className="card stack" aria-labelledby="standby-settings">
        <h2 id="standby-settings">{t("standby.settings")}</h2>
        <form
          className="stack"
          onSubmit={save}
          aria-label={t("standby.settings")}
        >
          <label className="checkbox">
            <input
              type="checkbox"
              checked={enabled}
              onChange={(e) => setEnabled(e.target.checked)}
            />
            {t("standby.enable")}
          </label>
          <label>
            {t("standby.interval")}
            <input
              type="number"
              min={5}
              max={1440}
              value={minutes}
              onChange={(e) => setMinutes(Number(e.target.value))}
            />
          </label>
          <p className="muted small">{t("standby.intervalHelp")}</p>
          <div className="actions">
            <button
              type="submit"
              disabled={busy || minutes < 5 || minutes > 1440}
            >
              {t("standby.save")}
            </button>
          </div>
        </form>
      </section>

      <section className="card stack" aria-labelledby="standby-agents">
        <h2 id="standby-agents">{t("standby.agents")}</h2>
        <table>
          <thead>
            <tr>
              <th>{t("standby.agent")}</th>
              <th>{t("standby.host")}</th>
              <th>{t("standby.role")}</th>
              <th>{t("standby.heartbeat")}</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {view.agents.map((a) => (
              <AgentRow key={a.id} agent={a} busy={busy} run={run} />
            ))}
          </tbody>
        </table>
        <p className="muted small">{t("standby.agentsHelp")}</p>
      </section>

      {active && <Promote agent={active} busy={busy} run={run} />}
    </>
  );
}

function AgentRow({
  agent,
  busy,
  run,
}: {
  agent: StandbyAgent;
  busy: boolean;
  run: (action: () => Promise<string>) => void;
}) {
  const { t } = useTranslation();
  const next = agent.role === "standby" ? "primary" : "standby";
  return (
    <tr>
      <td>{agent.name}</td>
      <td>{agent.hostname}</td>
      <td>{t(`standby.roleValue.${agent.role}`)}</td>
      <td title={formatDate(agent.last_heartbeat_at)}>
        {formatRelative(agent.last_heartbeat_at)}
      </td>
      <td>
        {agent.status === "active" && (
          <button
            type="button"
            className="small secondary"
            disabled={busy}
            onClick={() =>
              run(async () => {
                await api.setAgentRole(agent.id, next);
                return t("standby.roleChanged", {
                  agent: agent.name,
                  role: t(`standby.roleValue.${next}`),
                });
              })
            }
          >
            {t(`standby.makeRole.${next}`)}
          </button>
        )}
      </td>
    </tr>
  );
}

function Promote({
  agent,
  busy,
  run,
}: {
  agent: StandbyAgent;
  busy: boolean;
  run: (action: () => Promise<string>) => void;
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [confirm, setConfirm] = useState("");
  return (
    <section className="card stack" aria-labelledby="standby-promote">
      <h2 id="standby-promote">{t("standby.promoteTitle")}</h2>
      <ol className="small">
        <li>{t("standby.step1")}</li>
        <li>{t("standby.step2")}</li>
        <li>{t("standby.step3")}</li>
        <li>{t("standby.step4")}</li>
      </ol>
      {!open ? (
        <div className="actions">
          <button
            type="button"
            className="danger"
            onClick={() => setOpen(true)}
          >
            {t("standby.promote", { agent: agent.name })}
          </button>
        </div>
      ) : (
        <form
          className="stack"
          aria-label={t("standby.promoteTitle")}
          onSubmit={(e) => {
            e.preventDefault();
            run(async () => {
              const r = await api.promoteStandby(agent.id, confirm.trim());
              setOpen(false);
              return t("standby.promoted", { agent: r.promoted });
            });
          }}
        >
          <p className="warn-text">{t("standby.promoteWarning")}</p>
          <label>
            {t("standby.confirm", { agent: agent.name })}
            <input
              value={confirm}
              autoComplete="off"
              placeholder={agent.name}
              onChange={(e) => setConfirm(e.target.value)}
            />
          </label>
          <div className="actions">
            <button
              type="submit"
              className="danger"
              disabled={busy || confirm.trim() !== agent.name}
            >
              {t("standby.promoteConfirm")}
            </button>
            <button
              type="button"
              className="secondary"
              onClick={() => setOpen(false)}
            >
              {t("standby.cancel")}
            </button>
          </div>
        </form>
      )}
    </section>
  );
}
