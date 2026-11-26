import { useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";

import { api } from "../api/endpoints";
import type { AccountDetail } from "../api/types";
import { formatDate } from "../utils/format";
import { errorMessage } from "./common";
import { NoticeBar, type Notice } from "./notices";
import { PasswordForm } from "./PasswordForm";
import { QuotaForm } from "./QuotaForm";

type Panel = "archive" | "delete" | "quota" | "password" | null;

/** Unlock, archive (and empty) or delete a mailbox: global administrators only. */
export function MailboxActions({
  account,
  onChange,
}: {
  account: AccountDetail;
  onChange: () => void;
}) {
  const { t } = useTranslation();
  const [panel, setPanel] = useState<Panel>(null);
  const [notice, setNotice] = useState<Notice>(null);
  const [busy, setBusy] = useState(false);
  const [days, setDays] = useState(0);
  const [remove, setRemove] = useState(false);
  const [note, setNote] = useState("");
  const [confirm, setConfirm] = useState("");
  const [reason, setReason] = useState("");
  const confirmed =
    confirm.trim().toLowerCase() === account.email.toLowerCase();
  const backup = account.last_snapshot;

  async function run(action: () => Promise<string>) {
    setBusy(true);
    setNotice(null);
    try {
      setNotice({ kind: "ok", text: await action() });
      setPanel(null);
      setConfirm("");
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

  const unlock = () =>
    run(async () => {
      const r = await api.accountLock(account.id, "unlock");
      return r.queued ? t("mailbox.unlockQueued") : t("mailbox.alreadyPending");
    });

  const archive = (event: FormEvent) => {
    event.preventDefault();
    void run(async () => {
      await api.startLegalHold(account.id, note.trim(), {
        remove_from_mailbox: remove,
        older_than_days: days,
        confirm: remove ? confirm.trim() : "",
      });
      return remove
        ? t("mailbox.archiveRemoveQueued")
        : t("mailbox.archiveQueued");
    });
  };

  const remove_ = (event: FormEvent) => {
    event.preventDefault();
    void run(async () => {
      const r = await api.deleteMailbox(
        account.id,
        confirm.trim(),
        reason.trim(),
      );
      return r.queued ? t("mailbox.deleteQueued") : t("mailbox.alreadyPending");
    });
  };

  const confirmField: ReactNode = (
    <label className="grow">
      {t("mailbox.confirm", { email: account.email })}
      <input
        value={confirm}
        autoComplete="off"
        placeholder={account.email}
        onChange={(e) => setConfirm(e.target.value)}
      />
    </label>
  );

  return (
    <section className="card stack" aria-labelledby="mailbox-actions">
      <h2 id="mailbox-actions">{t("mailbox.title")}</h2>
      <div className="actions wrap-actions">
        {account.status === "locked" && (
          <button type="button" disabled={busy} onClick={() => void unlock()}>
            {t("mailbox.unlock")}
          </button>
        )}
        <button
          type="button"
          className="secondary"
          aria-expanded={panel === "quota"}
          onClick={() => setPanel(panel === "quota" ? null : "quota")}
        >
          {t("quota.title")}
        </button>
        <button
          type="button"
          className="secondary"
          aria-expanded={panel === "password"}
          onClick={() => setPanel(panel === "password" ? null : "password")}
        >
          {t("password.title")}
        </button>
        <button
          type="button"
          className="secondary"
          aria-expanded={panel === "archive"}
          onClick={() => setPanel(panel === "archive" ? null : "archive")}
        >
          {t("mailbox.archive")}
        </button>
        <button
          type="button"
          className="secondary danger"
          aria-expanded={panel === "delete"}
          onClick={() => setPanel(panel === "delete" ? null : "delete")}
        >
          {t("mailbox.delete")}
        </button>
      </div>
      {account.status === "locked" && (
        <p className="muted small">{t("mailbox.lockedHelp")}</p>
      )}
      <NoticeBar notice={notice} />

      {panel === "quota" && (
        <QuotaForm
          accountIds={[account.id]}
          current={account.quota_bytes ?? 0}
          onCancel={() => setPanel(null)}
          onDone={(text) => {
            setNotice({ kind: "ok", text });
            setPanel(null);
            onChange();
          }}
        />
      )}
      {panel === "password" && (
        <PasswordForm
          accountId={account.id}
          email={account.email}
          onCancel={() => setPanel(null)}
          onDone={(text) => {
            setNotice({ kind: "ok", text });
            setPanel(null);
            onChange();
          }}
        />
      )}
      {panel === "archive" && (
        <form
          className="stack"
          onSubmit={archive}
          aria-label={t("mailbox.archive")}
        >
          <p className="muted small">{t("mailbox.archiveHelp")}</p>
          <div className="row">
            <label>
              {t("mailbox.olderThan")}
              <input
                type="number"
                min={0}
                max={36500}
                value={days}
                onChange={(e) =>
                  setDays(Math.max(0, Number(e.target.value) || 0))
                }
              />
              <span className="hint">{t("mailbox.olderThanHint")}</span>
            </label>
            <label className="grow">
              {t("archives.legalNote")}
              <input
                maxLength={500}
                value={note}
                onChange={(e) => setNote(e.target.value)}
              />
            </label>
          </div>
          <label className="check">
            <input
              type="checkbox"
              checked={remove}
              onChange={(e) => setRemove(e.target.checked)}
            />
            {t("mailbox.removeAfter")}
          </label>
          {remove && (
            <>
              <p className="alert warn small">{t("mailbox.removeWarning")}</p>
              <div className="row">{confirmField}</div>
            </>
          )}
          <div className="actions">
            <button type="submit" disabled={busy || (remove && !confirmed)}>
              {remove
                ? t("mailbox.archiveRemoveButton")
                : t("mailbox.archiveButton")}
            </button>
          </div>
        </form>
      )}

      {panel === "delete" && (
        <form
          className="stack"
          onSubmit={remove_}
          aria-label={t("mailbox.delete")}
        >
          {backup ? (
            <p className="alert warn small">
              {t("mailbox.deleteWarning", {
                date: formatDate(backup.taken_at),
              })}
            </p>
          ) : (
            <p role="alert" className="alert error small">
              {t("mailbox.deleteNoBackup")}
            </p>
          )}
          <p className="muted small">
            {t("mailbox.deleteArchiveFirst")}{" "}
            <Link to="/archives">{t("nav.archives")}</Link>
          </p>
          <div className="row">
            {confirmField}
            <label className="grow">
              {t("mailbox.reason")}
              <input
                maxLength={500}
                value={reason}
                onChange={(e) => setReason(e.target.value)}
              />
            </label>
          </div>
          <div className="actions">
            <button
              type="submit"
              className="danger-solid"
              disabled={busy || !confirmed || !backup}
            >
              {t("mailbox.deleteButton")}
            </button>
          </div>
        </form>
      )}
    </section>
  );
}
