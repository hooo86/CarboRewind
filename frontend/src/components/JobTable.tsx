import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";

import type { Job } from "../api/types";
import { formatDate } from "../utils/format";
import { StatusBadge } from "./common";

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
                {t(`kind.${job.kind}`, { defaultValue: job.kind })}
                {job.backup_type && ` · ${t(`backupType.${job.backup_type}`)}`}
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
