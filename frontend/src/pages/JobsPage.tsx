import { useSearchParams } from "react-router-dom";
import { useTranslation } from "react-i18next";

import { api } from "../api/endpoints";
import type { Job, JobStatus, Page } from "../api/types";
import { JobTable } from "../components/JobTable";
import { LoadView, useLoad } from "../components/common";

const STATUSES: JobStatus[] = [
  "queued",
  "running",
  "succeeded",
  "failed",
  "cancelled",
];

export function JobsPage() {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const raw = params.get("status");
  const status = STATUSES.find((s) => s === raw);
  const [jobs, reload] = useLoad<Page<Job>>(
    () => api.jobs({ status }),
    [status],
    5000,
  );

  return (
    <>
      <h1>{t("jobs.title")}</h1>
      <div className="toolbar">
        <label>
          {t("jobs.filter")}
          <select
            value={status ?? ""}
            onChange={(e) =>
              setParams(e.target.value ? { status: e.target.value } : {}, {
                replace: true,
              })
            }
          >
            <option value="">{t("jobs.all")}</option>
            {STATUSES.map((s) => (
              <option key={s} value={s}>
                {t(`status.${s}`)}
              </option>
            ))}
          </select>
        </label>
        <span className="muted">{t("jobs.autoRefresh")}</span>
      </div>
      <LoadView load={jobs} reload={reload}>
        {(page) => <JobTable jobs={page.items} />}
      </LoadView>
    </>
  );
}
