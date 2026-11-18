import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import type { TFunction } from "i18next";

import type { Job, RestoreSummary } from "../api/types";
import { formatBytes, formatDate } from "../utils/format";
import { StatusBadge } from "./common";

/** One-line outcome of a finished restore ("4 restored, 2 already present…"). */
export function RestoreSummaryText({ job }: { job: Job }) {
  const { t } = useTranslation();
  const p = job.progress as Partial<RestoreSummary> | null;
  if (!p || p.restored === undefined) return null;
  const parts = [
    t(job.dry_run ? "restore.result.wouldRestore" : "restore.result.restored", {
      count: p.restored,
    }),
  ];
  if (p.present) parts.push(t("restore.result.present", { count: p.present }));
  if (p.unsupported)
    parts.push(t("restore.result.unsupported", { count: p.unsupported }));
  if (p.failed_count)
    parts.push(t("restore.result.failed", { count: p.failed_count }));
  return (
    <span className="summary">
      {parts.join(", ")}
      {p.root && (
        <>
          {" "}
          → <code>{p.root}</code>
        </>
      )}
    </span>
  );
}

function kindLabel(job: Job, t: TFunction): string {
  let label = t(`kind.${job.kind}`, { defaultValue: job.kind });
  if (job.backup_type) label += ` · ${t(`backupType.${job.backup_type}`)}`;
  if (job.kind === "restore") {
    if (job.dry_run) label += ` · ${t("restore.simulation")}`;
    if (job.snapshot_seq) label += ` · #${job.snapshot_seq}`;
  }
  return label;
}

function duration(job: Job): string {
  if (!job.started_at) return "—";
  const end = job.finished_at
    ? new Date(job.finished_at).getTime()
    : Date.now();
  const seconds = Math.max(
    0,
    Math.round((end - new Date(job.started_at).getTime()) / 1000),
  );
  if (seconds < 60) return `${seconds} s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} min ${seconds % 60} s`;
  return `${Math.floor(minutes / 60)} h ${minutes % 60} min`;
}

export function JobTable({
  jobs,
  showAccount = true,
}: {
  jobs: Job[];
  showAccount?: boolean;
}) {
  const { t } = useTranslation();
  if (jobs.length === 0) return <p className="muted">{t("jobs.empty")}</p>;
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>{t("jobs.created")}</th>
            {showAccount && <th>{t("jobs.account")}</th>}
            <th>{t("jobs.kind")}</th>
            <th>{t("jobs.status")}</th>
            <th>{t("jobs.duration")}</th>
            <th className="num">{t("jobs.attempts")}</th>
            <th>{t("jobs.error")}</th>
          </tr>
        </thead>
        <tbody>
          {jobs.map((job) => (
            <tr key={job.id}>
              <td>{formatDate(job.created_at)}</td>
              {showAccount && (
                <td>
                  {job.account_id && job.account_email ? (
                    <Link to={`/accounts/${job.account_id}`}>
                      {job.account_email}
                    </Link>
                  ) : (
                    "—"
                  )}
                </td>
              )}
              <td>
                {kindLabel(job, t)}
                {job.kind === "restore" && job.status === "succeeded" && (
                  <>
                    <br />
                    <RestoreSummaryText job={job} />
                  </>
                )}
                {job.kind === "purge" &&
                  job.status === "succeeded" &&
                  job.progress && (
                    <>
                      <br />
                      <span className="summary">
                        {t("purge.summary", {
                          blobs: Number(job.progress.blobs ?? 0),
                          manifests: Number(job.progress.manifests ?? 0),
                          freed: formatBytes(
                            Number(job.progress.freed_bytes ?? 0),
                          ),
                        })}
                      </span>
                    </>
                  )}
              </td>
              <td>
                <StatusBadge status={job.status} />
              </td>
              <td>{duration(job)}</td>
              <td className="num">{job.attempts}</td>
              <td className="error-cell" title={job.error ?? undefined}>
                {job.error ?? ""}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
