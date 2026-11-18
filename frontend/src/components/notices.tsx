import { useState } from "react";
import { useTranslation } from "react-i18next";

import type { Job } from "../api/types";

export type Notice = { kind: "ok" | "error"; text: string } | null;

export function NoticeBar({ notice }: { notice: Notice }) {
  if (!notice) return null;
  return (
    <p
      role={notice.kind === "error" ? "alert" : "status"}
      className={`alert ${notice.kind}`}
    >
      {notice.text}
    </p>
  );
}

export function useBackupNotices(onQueued?: () => void) {
  const { t } = useTranslation();
  const [notice, setNotice] = useState<Notice>(null);
  return {
    notice,
    queued: (job: Job) => {
      setNotice({
        kind: "ok",
        text: t("accounts.queued", {
          type: t(`backupType.${job.backup_type ?? "incremental"}`),
          email: job.account_email ?? "",
        }),
      });
      onQueued?.();
    },
    failed: (message: string) =>
      setNotice({ kind: "error", text: t("error", { message }) }),
  };
}
