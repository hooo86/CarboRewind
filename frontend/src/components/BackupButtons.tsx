import { useState } from "react";
import { useTranslation } from "react-i18next";

import { api } from "../api/endpoints";
import type { BackupType, Job } from "../api/types";
import { errorMessage } from "./common";

export function BackupButtons({
  accountId,
  email,
  onQueued,
  onError,
}: {
  accountId: string;
  email: string;
  onQueued: (job: Job) => void;
  onError: (message: string) => void;
}) {
  const { t } = useTranslation();
  const [busy, setBusy] = useState(false);

  async function start(type: BackupType) {
    setBusy(true);
    try {
      onQueued(await api.startBackup(accountId, type));
    } catch (err) {
      onError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  const button = (type: BackupType, label: string, cls: string) => (
    <button
      type="button"
      className={cls}
      disabled={busy}
      title={t("accounts.backupTitle", {
        type: t(`backupType.${type}`),
        email,
      })}
      aria-label={t("accounts.backupTitle", {
        type: t(`backupType.${type}`),
        email,
      })}
      onClick={() => void start(type)}
    >
      {label}
    </button>
  );

  return (
    <span className="actions">
      {button("incremental", t("accounts.backupIncremental"), "small")}
      {button("full", t("accounts.backupFull"), "small secondary")}
    </span>
  );
}
