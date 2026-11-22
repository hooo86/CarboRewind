import { useState } from "react";
import type { FormEvent } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { api } from "../api/endpoints";
import type { RestoreTestRun, RestoreTests } from "../api/types";
import { formatDate } from "../utils/format";
import { AccountPicker } from "./AccountPicker";
import { errorMessage, LoadView, StatusBadge, useLoad } from "./common";
import { NoticeBar, type Notice } from "./notices";
import { TimezoneSelect } from "./TimezoneSelect";

function History({ runs }: { runs: RestoreTestRun[] }) {
  const { t } = useTranslation();
  if (runs.length === 0)
    return <p className="muted">{t("restoreTest.noRun")}</p>;
  return (
    <div className="table-wrap">
      <table aria-label={t("restoreTest.history")}>
        <thead>
          <tr>
            <th>{t("restoreTest.date")}</th>
            <th>{t("restoreTest.source")}</th>
            <th>{t("restoreTest.result")}</th>
            <th className="num">{t("restoreTest.messages")}</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((r) => (
            <tr key={r.job_id}>
              <td>{formatDate(r.finished_at ?? r.created_at)}</td>
              <td>
                {r.source ?? "—"}
                {r.seq !== null && <span className="muted"> #{r.seq}</span>}
              </td>
              <td>
                {r.ok === true ? (
                  <span className="badge ok">{t("restoreTest.passed")}</span>
                ) : r.ok === false ? (
                  <span className="badge bad">
                    {t("restoreTest.failedBadge")}
                  </span>
                ) : (
                  <StatusBadge status={r.status} />
                )}
                {r.error && <p className="bad-text small">{r.error}</p>}
              </td>
              <td className="num">
                {r.restored ?? "—"} / {r.selected ?? "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Supervision: last runs, run now. Hidden when the API is unavailable. */
export function RestoreTestPanel() {
  const { t } = useTranslation();
  const [load, reload] = useLoad(() => api.restoreTests(), [], 30000);
  const [notice, setNotice] = useState<Notice>(null);
  if (load.state !== "ready") return null;
  const d = load.data;
  const last = d.history.find((r) => r.ok !== null);

  async function runNow() {
    setNotice(null);
    try {
      await api.runRestoreTest();
      setNotice({ kind: "ok", text: t("restoreTest.started") });
      reload();
    } catch (err) {
      setNotice({
        kind: "error",
        text: t("error", { message: errorMessage(err) }),
      });
    }
  }

  return (
    <section className="card stack" aria-labelledby="restore-test-title">
      <div className="toolbar">
        <h2 id="restore-test-title" className="grow">
          {t("restoreTest.title")}
        </h2>
        {d.settings.target_account_id && (
          <button
            type="button"
            className="secondary small"
            onClick={() => void runNow()}
          >
            {t("restoreTest.runNow")}
          </button>
        )}
        <Link className="button secondary small" to="/settings?tab=restoreTest">
          {t("restoreTest.settings")}
        </Link>
      </div>
      {!d.settings.target_account_id ? (
        <p className="muted">{t("restoreTest.notConfigured")}</p>
      ) : (
        <>
          {last?.ok === false && (
            <p role="alert" className="alert error">
              {t("restoreTest.lastFailed")}
            </p>
          )}
          <History runs={d.history.slice(0, 5)} />
        </>
      )}
      <NoticeBar notice={notice} />
    </section>
  );
}

function RestoreTestForm({
  initial,
  onSaved,
}: {
  initial: RestoreTests;
  onSaved: () => void;
}) {
  const { t } = useTranslation();
  const [form, setForm] = useState(initial.settings);
  const [target, setTarget] = useState(initial.target_email ?? "");
  const [notice, setNotice] = useState<Notice>(null);

  async function save(event: FormEvent) {
    event.preventDefault();
    setNotice(null);
    try {
      let targetId: string | null = null;
      if (target.trim()) {
        const page = await api.accountList({
          q: target.trim(),
          filters: [],
          sort: "email",
          order: "asc",
          limit: 20,
          offset: 0,
        });
        const found = page.items.find(
          (a) => a.email.toLowerCase() === target.trim().toLowerCase(),
        );
        if (!found) throw new Error(t("restoreTest.unknownMailbox"));
        targetId = found.id;
      }
      await api.saveRestoreTests({ ...form, target_account_id: targetId });
      setNotice({ kind: "ok", text: t("restoreTest.saved") });
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
      onSubmit={(e) => void save(e)}
      aria-label={t("restoreTest.title")}
    >
      <h2>{t("restoreTest.title")}</h2>
      <p className="muted">{t("restoreTest.intro")}</p>
      <label className="inline">
        <input
          type="checkbox"
          checked={form.enabled}
          onChange={(e) => setForm({ ...form, enabled: e.target.checked })}
        />
        {t("restoreTest.enabled")}
      </label>
      <div className="row">
        <AccountPicker
          label={t("restoreTest.target")}
          value={target}
          onChange={setTarget}
        />
        <label>
          {t("restoreTest.count")}
          <input
            type="number"
            min={1}
            max={500}
            value={form.messages}
            onChange={(e) =>
              setForm({ ...form, messages: Number(e.target.value) })
            }
          />
        </label>
      </div>
      <p className="muted small">{t("restoreTest.targetHelp")}</p>
      <div className="row">
        <label className="grow">
          {t("restoreTest.schedule")}
          <input
            value={form.schedule}
            onChange={(e) => setForm({ ...form, schedule: e.target.value })}
          />
        </label>
        <label>
          {t("monitoring.timezone")}
          <TimezoneSelect
            value={form.timezone}
            onChange={(timezone) => setForm({ ...form, timezone })}
          />
        </label>
      </div>
      <label className="inline">
        <input
          type="checkbox"
          checked={form.cleanup}
          onChange={(e) => setForm({ ...form, cleanup: e.target.checked })}
        />
        {t("restoreTest.cleanup")}
      </label>
      <NoticeBar notice={notice} />
      <div className="actions">
        <button type="submit">{t("save")}</button>
      </div>
      <h3>{t("restoreTest.history")}</h3>
      <History runs={initial.history} />
    </form>
  );
}

export function RestoreTestSettingsTab() {
  const [load, reload] = useLoad(() => api.restoreTests(), []);
  return (
    <LoadView load={load} reload={reload}>
      {(d) => <RestoreTestForm initial={d} onSaved={reload} />}
    </LoadView>
  );
}
