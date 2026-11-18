import { useTranslation } from "react-i18next";

import { api } from "../api/endpoints";
import type { Dashboard, Job, Page } from "../api/types";
import { JobTable } from "../components/JobTable";
import { LoadView, useLoad } from "../components/common";
import { formatBytes, formatRelative } from "../utils/format";

function Tile({
  label,
  value,
  tone,
}: {
  label: string;
  value: string | number;
  tone?: string;
}) {
  return (
    <div className={`tile ${tone ?? ""}`}>
      <span className="tile-value">{value}</span>
      <span className="tile-label">{label}</span>
    </div>
  );
}

export function DashboardPage() {
  const { t } = useTranslation();
  const [stats, reloadStats] = useLoad<Dashboard>(
    () => api.dashboard(),
    [],
    15000,
  );
  const [jobs, reloadJobs] = useLoad<Page<Job>>(() => api.jobs(), [], 5000);

  return (
    <>
      <h1>{t("dashboard.title")}</h1>
      <LoadView load={stats} reload={reloadStats}>
        {(d) => (
          <>
            <section className="tiles" aria-label={t("dashboard.title")}>
              <Tile label={t("dashboard.accounts")} value={d.accounts} />
              <Tile
                label={t("dashboard.protected")}
                value={d.protected_24h}
                tone="ok"
              />
              <Tile
                label={t("dashboard.never")}
                value={d.never_backed_up}
                tone={d.never_backed_up ? "warn" : undefined}
              />
              <Tile
                label={t("dashboard.partial")}
                value={d.partial_last}
                tone={d.partial_last ? "warn" : undefined}
              />
              <Tile label={t("dashboard.queued")} value={d.jobs_queued} />
              <Tile label={t("dashboard.running")} value={d.jobs_running} />
              <Tile
                label={t("dashboard.failed")}
                value={d.jobs_failed_24h}
                tone={d.jobs_failed_24h ? "bad" : undefined}
              />
              <Tile
                label={t("dashboard.stored")}
                value={formatBytes(d.stored_bytes)}
              />
              {d.agents_active !== null && (
                <Tile
                  label={t("dashboard.agents")}
                  value={d.agents_active}
                  tone={d.agents_active ? "ok" : "bad"}
                />
              )}
            </section>
            {d.last_agent_heartbeat && (
              <p className="muted">
                {t("dashboard.heartbeat", {
                  when: formatRelative(d.last_agent_heartbeat),
                })}
              </p>
            )}
          </>
        )}
      </LoadView>
      <h2>{t("dashboard.recentJobs")}</h2>
      <LoadView load={jobs} reload={reloadJobs}>
        {(page) => <JobTable jobs={page.items.slice(0, 10)} />}
      </LoadView>
    </>
  );
}
