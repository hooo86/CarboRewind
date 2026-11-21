import { useState } from "react";
import { useTranslation } from "react-i18next";

import { api } from "../api/endpoints";
import type {
  MailQueueAgent,
  MailQueueDetail,
  MailQueueOp,
} from "../api/types";
import { formatDate, formatRelative } from "../utils/format";
import { errorMessage, useLoad } from "./common";
import { NoticeBar, type Notice } from "./notices";

const REFRESH_MS = 20_000;

function QueueTable({
  agent,
  detail,
  onDone,
}: {
  agent: MailQueueAgent;
  detail: MailQueueDetail;
  onDone: (notice: Notice) => void;
}) {
  const { t } = useTranslation();
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const ids = detail.items.map((m) => m.id);
  const all = ids.length > 0 && ids.every((id) => selected.has(id));

  function toggle(id: string) {
    const next = new Set(selected);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setSelected(next);
  }

  async function run(op: MailQueueOp) {
    const chosen = ids.filter((id) => selected.has(id));
    let confirm: number | undefined;
    if (op === "delete") {
      const typed = window.prompt(
        t("queue.confirmDelete", { count: chosen.length }),
      );
      if (typed === null) return;
      confirm = Number(typed.trim());
      if (confirm !== chosen.length) {
        onDone({ kind: "error", text: t("queue.confirmMismatch") });
        return;
      }
    }
    setBusy(true);
    try {
      await api.mailQueueAction({
        agent_id: agent.agent_id,
        op,
        queue: detail.queue,
        ids: chosen,
        server: detail.server,
        confirm,
      });
      setSelected(new Set());
      onDone({ kind: "ok", text: t("queue.sent") });
    } catch (err) {
      onDone({
        kind: "error",
        text: t("error", { message: errorMessage(err) }),
      });
    } finally {
      setBusy(false);
    }
  }

  const none = selected.size === 0 || busy;
  return (
    <div className="stack">
      <div className="toolbar">
        <h3 className="grow">
          {t(`queue.name.${detail.queue}`, { defaultValue: detail.queue })} —{" "}
          {t("queue.count", { count: detail.total })}
          {detail.total > detail.items.length &&
            ` (${t("queue.shown", { count: detail.items.length })})`}
        </h3>
        <button
          type="button"
          className="secondary small"
          disabled={none}
          onClick={() => void run("requeue")}
        >
          {t("queue.requeue")}
        </button>
        {detail.queue === "hold" ? (
          <button
            type="button"
            className="secondary small"
            disabled={none}
            onClick={() => void run("release")}
          >
            {t("queue.release")}
          </button>
        ) : (
          <button
            type="button"
            className="secondary small"
            disabled={none}
            onClick={() => void run("hold")}
          >
            {t("queue.hold")}
          </button>
        )}
        <button
          type="button"
          className="danger small"
          disabled={none}
          onClick={() => void run("delete")}
        >
          {t("queue.delete")}
        </button>
      </div>
      {detail.reasons.length > 0 && (
        <ul className="services" aria-label={t("queue.reasons")}>
          {detail.reasons.map((r) => (
            <li key={r.reason} className="badge muted">
              {r.count} × {r.reason}
            </li>
          ))}
        </ul>
      )}
      <div className="table-wrap">
        <table aria-label={t("queue.messagesOf", { queue: detail.queue })}>
          <thead>
            <tr>
              <th>
                <input
                  type="checkbox"
                  aria-label={t("queue.selectAll")}
                  checked={all}
                  onChange={() => setSelected(all ? new Set() : new Set(ids))}
                />
              </th>
              <th>{t("queue.date")}</th>
              <th>{t("queue.from")}</th>
              <th>{t("queue.to")}</th>
              <th>{t("queue.reason")}</th>
            </tr>
          </thead>
          <tbody>
            {detail.items.map((m) => (
              <tr key={m.id}>
                <td>
                  <input
                    type="checkbox"
                    aria-label={t("queue.select", { id: m.id })}
                    checked={selected.has(m.id)}
                    onChange={() => toggle(m.id)}
                  />
                </td>
                <td title={m.id}>
                  {m.time
                    ? formatRelative(new Date(m.time * 1000).toISOString())
                    : "—"}
                </td>
                <td>{m.from || "<>"}</td>
                <td>{m.to.split(",").join(", ")}</td>
                <td className="small">{m.reason}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/** Supervision: messages waiting in the MTA queues, with requeue / hold / delete. */
export function MailQueuePanel() {
  const { t } = useTranslation();
  const [load, reload] = useLoad(() => api.mailQueue(), [], REFRESH_MS);
  const [notice, setNotice] = useState<Notice>(null);
  const [busy, setBusy] = useState(false);
  if (load.state !== "ready" || load.data.length === 0) return null;

  async function flush(agent: MailQueueAgent) {
    setBusy(true);
    try {
      await api.mailQueueAction({ agent_id: agent.agent_id, op: "flush" });
      setNotice({ kind: "ok", text: t("queue.sent") });
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
    <section className="card stack" aria-labelledby="queue-title">
      <h2 id="queue-title">{t("queue.title")}</h2>
      {load.data.map((agent) => {
        const details = (agent.details ?? []).filter((d) => d.total > 0);
        return (
          <div key={agent.agent_id} className="stack">
            <div className="toolbar">
              <p className="muted grow">
                {t("queue.of", {
                  agent: agent.agent,
                  date: agent.checked_at ? formatDate(agent.checked_at) : "—",
                })}
                {" — "}
                {agent.queue_total === null
                  ? t("queue.unavailable")
                  : agent.queue_total === 0
                    ? t("queue.empty")
                    : Object.entries(agent.queue ?? {})
                        .filter(([, n]) => n > 0)
                        .map(
                          ([q, n]) =>
                            `${t(`queue.name.${q}`, { defaultValue: q })} : ${n}`,
                        )
                        .join(", ")}
              </p>
              <button
                type="button"
                className="secondary small"
                disabled={busy || !agent.queue_total}
                onClick={() => void flush(agent)}
              >
                {t("queue.flush")}
              </button>
            </div>
            {agent.details === null && agent.queue_total ? (
              <p className="muted small">{t("queue.agentTooOld")}</p>
            ) : null}
            {details.map((d) => (
              <QueueTable
                key={`${d.server}-${d.queue}`}
                agent={agent}
                detail={d}
                onDone={(n) => {
                  setNotice(n);
                  reload();
                }}
              />
            ))}
            {agent.pending.length > 0 && (
              <p className="muted small" role="status">
                {t("queue.pending", { count: agent.pending.length })}
              </p>
            )}
            {agent.results.length > 0 && (
              <details>
                <summary>{t("queue.history")}</summary>
                <ul className="small">
                  {agent.results.slice(0, 10).map((r) => (
                    <li key={r.id}>
                      {formatDate(r.done_at)} — {t(`queue.op.${r.op}`)}
                      {r.ids.length > 0 && ` (${r.ids.length})`} — {r.by}
                      {" — "}
                      {r.error ? (
                        <span className="bad-text">{r.error}</span>
                      ) : (
                        t("queue.ok")
                      )}
                    </li>
                  ))}
                </ul>
              </details>
            )}
          </div>
        );
      })}
      <NoticeBar notice={notice} />
    </section>
  );
}
