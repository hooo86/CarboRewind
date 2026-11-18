import { useEffect } from "react";
import { Link, useParams } from "react-router-dom";
import { useTranslation } from "react-i18next";

import { api } from "../api/endpoints";
import type { AccountDetail, Job, Page, Snapshot } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { BackupButtons } from "../components/BackupButtons";
import { JobTable } from "../components/JobTable";
import { LoadView, StatusBadge, useLoad } from "../components/common";
import { formatBytes, formatDate } from "../utils/format";
import { NoticeBar, useBackupNotices } from "./AccountsPage";

function SnapshotTable({
  snapshots,
  accountId,
  restorable,
}: {
  snapshots: Snapshot[];
  accountId: string;
  restorable: boolean;
}) {
  const { t } = useTranslation();
  if (snapshots.length === 0)
    return <p className="muted">{t("account.noSnapshot")}</p>;
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th className="num">{t("account.seq")}</th>
            <th>{t("account.takenAt")}</th>
            <th>{t("account.type")}</th>
            <th>{t("account.status")}</th>
            <th className="num">{t("account.items")}</th>
            <th className="num">{t("account.size")}</th>
            <th className="num">{t("account.newBytes")}</th>
            {restorable && <th>{t("accounts.actions")}</th>}
          </tr>
        </thead>
        <tbody>
          {snapshots.map((s) => (
            <tr key={s.id}>
              <td className="num">{s.seq}</td>
              <td>{formatDate(s.taken_at)}</td>
              <td>{t(`backupType.${s.backup_type}`)}</td>
              <td>
                <StatusBadge status={s.status} />
              </td>
              <td className="num">{s.item_count.toLocaleString()}</td>
              <td className="num">{formatBytes(s.size_bytes)}</td>
              <td className="num">{formatBytes(s.new_bytes)}</td>
              {restorable && (
                <td>
                  {s.status !== "expired" && (
                    <Link
                      className="button-link"
                      to={`/accounts/${accountId}/snapshots/${s.id}/restore`}
                      aria-label={t("account.restoreTitle", { seq: s.seq })}
                    >
                      {t("account.restore")}
                    </Link>
                  )}
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function AccountPage() {
  const { id = "" } = useParams();
  const { t } = useTranslation();
  const { canRunBackup, canRestore } = useAuth();
  const [account, reloadAccount] = useLoad<AccountDetail>(
    () => api.account(id),
    [id],
  );
  const [snapshots, reloadSnapshots] = useLoad<Page<Snapshot>>(
    () => api.snapshots(id),
    [id],
  );
  const [jobs, reloadJobs] = useLoad<Page<Job>>(
    () => api.jobs({ account_id: id }),
    [id],
    5000,
  );
  const { notice, queued, failed } = useBackupNotices(reloadJobs);

  // Refresh snapshots when a job of this account finishes.
  const lastFinished =
    jobs.state === "ready"
      ? (jobs.data.items.find((j) => j.finished_at)?.id ?? "")
      : "";
  useEffect(() => {
    if (lastFinished) {
      reloadSnapshots();
      reloadAccount();
    }
  }, [lastFinished, reloadSnapshots, reloadAccount]);

  return (
    <>
      <p>
        <Link to="/accounts">{t("account.back")}</Link>
      </p>
      <LoadView load={account} reload={reloadAccount}>
        {(a) => (
          <>
            <div className="page-head">
              <h1>{a.email}</h1>
              {canRunBackup(a.domain_id) && (
                <div>
                  <span className="muted">{t("account.backupNow")} : </span>
                  <BackupButtons
                    accountId={a.id}
                    email={a.email}
                    onQueued={queued}
                    onError={failed}
                  />
                </div>
              )}
            </div>
            <NoticeBar notice={notice} />
            <dl className="facts">
              <div>
                <dt>{t("account.domain")}</dt>
                <dd>{a.domain}</dd>
              </div>
              <div>
                <dt>{t("account.status")}</dt>
                <dd>
                  <StatusBadge status={a.status} />
                </dd>
              </div>
              <div>
                <dt>{t("account.lastSeen")}</dt>
                <dd>{formatDate(a.last_seen_at)}</dd>
              </div>
              <div>
                <dt>{t("account.snapshotCount")}</dt>
                <dd>{a.snapshot_count}</dd>
              </div>
            </dl>
          </>
        )}
      </LoadView>
      <h2>{t("account.snapshots")}</h2>
      <LoadView load={snapshots} reload={reloadSnapshots}>
        {(page) => (
          <SnapshotTable
            snapshots={page.items}
            accountId={id}
            restorable={
              account.state === "ready" && canRestore(account.data.domain_id)
            }
          />
        )}
      </LoadView>
      <h2>{t("account.jobs")}</h2>
      <LoadView load={jobs} reload={reloadJobs}>
        {(page) => <JobTable jobs={page.items} showAccount={false} />}
      </LoadView>
    </>
  );
}
