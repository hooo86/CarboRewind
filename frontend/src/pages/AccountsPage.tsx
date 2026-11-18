import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useTranslation } from "react-i18next";

import { api } from "../api/endpoints";
import type { AccountRow, Domain, Job, Page } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { BackupButtons } from "../components/BackupButtons";
import { LoadView, StatusBadge, useLoad } from "../components/common";
import { formatDate, formatRelative } from "../utils/format";

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

function AccountList({ domain, q }: { domain: Domain; q: string }) {
  const { t } = useTranslation();
  const { canRunBackup } = useAuth();
  const [offset, setOffset] = useState(0);
  useEffect(() => setOffset(0), [domain.id, q]);
  const [load, reload] = useLoad<Page<AccountRow>>(
    () => api.accounts(domain.id, q, offset),
    [domain.id, q, offset],
  );
  const { notice, queued, failed } = useBackupNotices();
  const allowed = canRunBackup(domain.id);

  return (
    <>
      <NoticeBar notice={notice} />
      <LoadView load={load} reload={reload}>
        {(page) =>
          page.items.length === 0 ? (
            <p className="muted">{t("accounts.empty")}</p>
          ) : (
            <>
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>{t("accounts.email")}</th>
                      <th>{t("accounts.status")}</th>
                      <th>{t("accounts.lastBackup")}</th>
                      {allowed && <th>{t("accounts.actions")}</th>}
                    </tr>
                  </thead>
                  <tbody>
                    {page.items.map((a) => (
                      <tr key={a.id}>
                        <td>
                          <Link to={`/accounts/${a.id}`}>{a.email}</Link>
                        </td>
                        <td>
                          <StatusBadge status={a.status} />
                        </td>
                        <td>
                          {a.last_backup_at ? (
                            <span title={formatDate(a.last_backup_at)}>
                              {formatRelative(a.last_backup_at)}{" "}
                              <StatusBadge status={a.last_backup_status} />
                            </span>
                          ) : (
                            <span className="muted">{t("accounts.never")}</span>
                          )}
                        </td>
                        {allowed && (
                          <td>
                            <BackupButtons
                              accountId={a.id}
                              email={a.email}
                              onQueued={queued}
                              onError={failed}
                            />
                          </td>
                        )}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="pager">
                <span className="muted">
                  {t("accounts.count", {
                    shown: `${page.offset + 1}–${page.offset + page.items.length}`,
                    total: page.total,
                  })}
                </span>
                <button
                  type="button"
                  className="secondary small"
                  disabled={page.offset === 0}
                  onClick={() =>
                    setOffset(Math.max(0, page.offset - page.limit))
                  }
                >
                  {t("accounts.previous")}
                </button>
                <button
                  type="button"
                  className="secondary small"
                  disabled={page.offset + page.items.length >= page.total}
                  onClick={() => setOffset(page.offset + page.limit)}
                >
                  {t("accounts.next")}
                </button>
              </div>
            </>
          )
        }
      </LoadView>
    </>
  );
}

export function AccountsPage() {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const [domains, reload] = useLoad<Page<Domain>>(() => api.domains(), []);
  const [draft, setDraft] = useState(params.get("q") ?? "");
  const q = params.get("q") ?? "";

  return (
    <>
      <h1>{t("accounts.title")}</h1>
      <LoadView load={domains} reload={reload}>
        {(page) => {
          const first = page.items[0];
          if (!first) return <p className="muted">{t("accounts.noDomain")}</p>;
          const domain =
            page.items.find((d) => d.id === params.get("domain")) ?? first;
          const update = (changes: Record<string, string>) => {
            const next = new URLSearchParams(params);
            for (const [k, v] of Object.entries(changes)) {
              if (v) next.set(k, v);
              else next.delete(k);
            }
            setParams(next, { replace: true });
          };
          return (
            <>
              <form
                className="toolbar"
                role="search"
                onSubmit={(e) => {
                  e.preventDefault();
                  update({ q: draft.trim() });
                }}
              >
                <label>
                  {t("accounts.domain")}
                  <select
                    value={domain.id}
                    onChange={(e) => update({ domain: e.target.value })}
                  >
                    {page.items.map((d) => (
                      <option key={d.id} value={d.id}>
                        {d.name} ({d.account_count})
                      </option>
                    ))}
                  </select>
                </label>
                <label className="grow">
                  {t("accounts.search")}
                  <input
                    type="search"
                    value={draft}
                    placeholder={t("accounts.searchPlaceholder")}
                    onChange={(e) => setDraft(e.target.value)}
                  />
                </label>
                <button type="submit" className="secondary">
                  {t("accounts.search")}
                </button>
              </form>
              <AccountList domain={domain} q={q} />
            </>
          );
        }}
      </LoadView>
    </>
  );
}
