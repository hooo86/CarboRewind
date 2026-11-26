import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useTranslation } from "react-i18next";

import { api } from "../api/endpoints";
import type { Job, JobStatus, Page } from "../api/types";
import { JobTable, type JobSort } from "../components/JobTable";
import { LoadView, useLoad } from "../components/common";

const STATUSES: JobStatus[] = [
  "queued",
  "running",
  "succeeded",
  "failed",
  "cancelled",
];
const KINDS = [
  "backup",
  "restore",
  "archive",
  "migrate",
  "replicate",
  "verify",
  "purge",
];
const SORTS: JobSort[] = [
  "created_at",
  "account",
  "kind",
  "status",
  "duration",
];
const SIZES = [25, 50, 100, 200];

export function JobsPage() {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const status = STATUSES.find((s) => s === params.get("status"));
  const kind = KINDS.find((k) => k === params.get("kind"));
  const sort = SORTS.find((s) => s === params.get("sort")) ?? "created_at";
  const order = params.get("order") === "asc" ? "asc" : "desc";
  const limit = SIZES.find((n) => String(n) === params.get("limit")) ?? 50;
  const offset = Math.max(0, Number(params.get("offset")) || 0);
  const q = params.get("q") ?? "";
  const [account, setAccount] = useState(q);
  useEffect(() => setAccount(q), [q]);

  const [jobs, reload] = useLoad<Page<Job>>(
    () =>
      api.jobs({ status, kind, q: q || undefined, sort, order, limit, offset }),
    [status, kind, q, sort, order, limit, offset],
    5000,
  );

  /** Change some parameters; any filter change goes back to the first page. */
  const update = (
    changes: Record<string, string | undefined>,
    keepPage = false,
  ) => {
    const next = new URLSearchParams(params);
    for (const [key, value] of Object.entries(changes)) {
      if (value) next.set(key, value);
      else next.delete(key);
    }
    if (!keepPage) next.delete("offset");
    setParams(next, { replace: true });
  };

  return (
    <>
      <h1>{t("jobs.title")}</h1>
      <div className="toolbar">
        <label>
          {t("jobs.filter")}
          <select
            value={status ?? ""}
            onChange={(e) => update({ status: e.target.value || undefined })}
          >
            <option value="">{t("jobs.all")}</option>
            {STATUSES.map((s) => (
              <option key={s} value={s}>
                {t(`status.${s}`)}
              </option>
            ))}
          </select>
        </label>
        <label>
          {t("jobs.kindFilter")}
          <select
            value={kind ?? ""}
            onChange={(e) => update({ kind: e.target.value || undefined })}
          >
            <option value="">{t("jobs.allKinds")}</option>
            {KINDS.map((k) => (
              <option key={k} value={k}>
                {t(`kind.${k}`, { defaultValue: k })}
              </option>
            ))}
          </select>
        </label>
        <form
          className="grow"
          onSubmit={(e) => {
            e.preventDefault();
            update({ q: account.trim() || undefined });
          }}
        >
          <label>
            {t("jobs.accountFilter")}
            <input
              type="search"
              value={account}
              placeholder={t("jobs.accountPlaceholder")}
              onChange={(e) => setAccount(e.target.value)}
              onBlur={() => update({ q: account.trim() || undefined })}
            />
          </label>
        </form>
        <span className="muted small">{t("jobs.autoRefresh")}</span>
      </div>
      <LoadView load={jobs} reload={reload}>
        {(page) => (
          <>
            <JobTable
              jobs={page.items}
              sort={sort}
              order={order}
              onSort={(column) =>
                update(
                  {
                    sort: column,
                    order:
                      column === sort
                        ? order === "asc"
                          ? "desc"
                          : "asc"
                        : column === "account" || column === "kind"
                          ? "asc"
                          : "desc",
                  },
                  false,
                )
              }
            />
            <div className="pager">
              <span className="muted">
                {t("jobs.range", {
                  from: page.total ? offset + 1 : 0,
                  to: Math.min(offset + limit, page.total),
                  total: page.total,
                })}
              </span>
              <label className="inline">
                {t("jobs.perPage")}
                <select
                  value={limit}
                  onChange={(e) => update({ limit: e.target.value })}
                >
                  {SIZES.map((n) => (
                    <option key={n} value={n}>
                      {n}
                    </option>
                  ))}
                </select>
              </label>
              <button
                type="button"
                className="secondary small"
                disabled={offset === 0}
                onClick={() =>
                  update({ offset: String(Math.max(0, offset - limit)) }, true)
                }
              >
                {t("jobs.previous")}
              </button>
              <button
                type="button"
                className="secondary small"
                disabled={offset + limit >= page.total}
                onClick={() => update({ offset: String(offset + limit) }, true)}
              >
                {t("jobs.next")}
              </button>
            </div>
          </>
        )}
      </LoadView>
    </>
  );
}
