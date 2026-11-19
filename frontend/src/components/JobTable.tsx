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
  if (job.kind === "migrate") {
    if (job.dry_run) label += ` · ${t("migrations.test")}`;
    if (job.source_email) label += ` · ${job.source_email}`;
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

export type JobSort = "created_at" | "account" | "kind" | "status" | "duration";

/** "12,4 MB/s" (bytes per second, shown in decimal megabytes like transfer tools). */
export function formatRate(bps: number | null | undefined): string {
  if (!bps) return "—";
  if (bps >= 1_000_000) return `${(bps / 1_000_000).toFixed(1)} MB/s`;
  return `${Math.max(1, Math.round(bps / 1000))} kB/s`;
}

export function JobTable({
  jobs,
  showAccount = true,
  sort,
  order,
  onSort,
}: {
  jobs: Job[];
  showAccount?: boolean;
  sort?: JobSort;
  order?: "asc" | "desc";
  onSort?: (column: JobSort) => void;
}) {
  const { t } = useTranslation();
  if (jobs.length === 0) return <p className="muted">{t("jobs.empty")}</p>;
  const head = (column: JobSort, label: string, className?: string) => (
    <th
      className={className}
      aria-sort={
        sort === column
          ? order === "asc"
            ? "ascending"
            : "descending"
          : undefined
      }
    >
      {onSort ? (
        <button
          type="button"
          className="th-sort"
          title={t("jobs.sortBy", { column: label })}
          onClick={() => onSort(column)}
        >
          {label}
          {sort === column ? (order === "asc" ? " ▲" : " ▼") : ""}
        </button>
      ) : (
        label
      )}
    </th>
  );
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            {head("created_at", t("jobs.created"))}
            {showAccount && head("account", t("jobs.account"))}
            {head("kind", t("jobs.kind"))}
            {head("status", t("jobs.status"))}
            {head("duration", t("jobs.duration"))}
            <th className="num">{t("jobs.throughput")}</th>
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
                {job.kind === "migrate" && job.progress && (
                  <>
                    <br />
                    <span className="summary">
                      {t("migrations.jobSummary", {
                        copied: Number(job.progress.copied ?? 0),
                        failed: Number(
                          job.progress.failed_count ?? job.progress.failed ?? 0,
                        ),
                      })}
                    </span>
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
              <td className="num">
                {formatRate(job.throughput_bps)}
                {!!job.bytes_done && (
                  <span className="muted small">
                    <br />
                    {t("jobs.read", { bytes: formatBytes(job.bytes_done) })}
                  </span>
                )}
              </td>
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
