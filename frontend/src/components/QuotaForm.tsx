import { useState } from "react";
import type { FormEvent } from "react";
import { useTranslation } from "react-i18next";

import { api } from "../api/endpoints";
import { formatBytes } from "../utils/format";
import { errorMessage } from "./common";
import { NoticeBar, type Notice } from "./notices";

const GB = 1024 ** 3;

/** Set the mail quota of one or several accounts (applied by the agent within a minute). */
export function QuotaForm({
  accountIds,
  current,
  onDone,
  onCancel,
}: {
  accountIds: string[];
  /** Quota of the account when only one is edited (null or 0: unlimited). */
  current?: number | null;
  onDone: (message: string) => void;
  onCancel?: () => void;
}) {
  const { t } = useTranslation();
  const [unlimited, setUnlimited] = useState(current === 0);
  const [gb, setGb] = useState(
    current ? String(Math.round((current / GB) * 100) / 100) : "",
  );
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<Notice>(null);
  const value = Number(gb.replace(",", "."));
  const valid = unlimited || (gb.trim() !== "" && value > 0 && value <= 100000);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!valid) return;
    setBusy(true);
    setNotice(null);
    try {
      const bytes = unlimited ? 0 : Math.round(value * GB);
      const r = await api.setQuota(accountIds, bytes, reason.trim());
      onDone(
        t("quota.queued", {
          count: r.queued,
          quota: unlimited ? t("quota.unlimited") : formatBytes(bytes),
          unchanged: r.unchanged,
          deleted: r.skipped_deleted,
        }),
      );
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
    <form
      className="stack"
      onSubmit={(e) => void submit(e)}
      aria-label={t("quota.title")}
    >
      <p className="muted small">
        {accountIds.length === 1 && current !== undefined
          ? t("quota.current", {
              quota: current ? formatBytes(current) : t("quota.unlimited"),
            })
          : t("quota.help", { count: accountIds.length })}
      </p>
      <div className="row">
        <label>
          {t("quota.size")}
          <input
            type="number"
            min={0.01}
            step="any"
            inputMode="decimal"
            value={gb}
            disabled={unlimited}
            required={!unlimited}
            onChange={(e) => setGb(e.target.value)}
          />
        </label>
        <label className="checkbox">
          <input
            type="checkbox"
            checked={unlimited}
            onChange={(e) => setUnlimited(e.target.checked)}
          />
          {t("quota.unlimited")}
        </label>
        <label className="grow">
          {t("quota.reason")}
          <input
            value={reason}
            maxLength={500}
            onChange={(e) => setReason(e.target.value)}
          />
        </label>
      </div>
      <NoticeBar notice={notice} />
      <div className="actions">
        <button type="submit" disabled={busy || !valid}>
          {t("quota.apply", { count: accountIds.length })}
        </button>
        {onCancel && (
          <button type="button" className="secondary" onClick={onCancel}>
            {t("quota.cancel")}
          </button>
        )}
      </div>
    </form>
  );
}
