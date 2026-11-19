import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { api } from "../api/endpoints";
import { useAuth } from "../auth/AuthContext";
import { errorMessage } from "../components/common";

type Source = "api" | "scheduler" | "agent" | "mail" | "mailbox";
const SOURCES: Source[] = ["api", "scheduler", "agent", "mail", "mailbox"];
const SIZES = [100, 500, 1000, 2000];
const LEVELS = ["", "WARNING", "ERROR"] as const;
const RANK: Record<string, number> = {
  DEBUG: 10,
  INFO: 20,
  WARNING: 30,
  ERROR: 40,
  CRITICAL: 50,
};

function serverLine(entry: Record<string, unknown>): string {
  const { ts, level, logger, msg, exc, ...extra } = entry;
  const rest = Object.keys(extra).length ? ` ${JSON.stringify(extra)}` : "";
  const head = [ts, level, logger].filter(Boolean).join(" ");
  return `${head} ${String(msg ?? "")}${rest}${exc ? `\n${String(exc)}` : ""}`;
}

/** Logs of CarboRewind (API, scheduler, agent) and Carbonio (MTA, mailbox), read-only. */
export function LogsPage() {
  const { t } = useTranslation();
  const { canManageSettings } = useAuth();
  const [source, setSource] = useState<Source>("api");
  const [lines, setLines] = useState(500);
  const [level, setLevel] = useState<(typeof LEVELS)[number]>("");
  const [filter, setFilter] = useState("");
  const [text, setText] = useState<string[] | null>(null);
  const [state, setState] = useState<"idle" | "loading" | "error">("idle");
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const [auto, setAuto] = useState(false);
  const server = source === "api" || source === "scheduler";

  useEffect(() => {
    if (!canManageSettings) return;
    let cancelled = false;
    let timer: number | undefined;
    setState("loading");
    setError(null);
    const done = (result: string[]) => {
      if (cancelled) return;
      setText(result);
      setState("idle");
    };
    const fail = (err: unknown) => {
      if (cancelled) return;
      setError(errorMessage(err));
      setState("error");
    };
    if (server) {
      api
        .serverLog(source, lines)
        .then((r) => {
          if (!r.available) throw new Error(t("logs.noRedis"));
          done(
            r.lines
              .filter(
                (l) =>
                  !level || (RANK[String(l.level)] ?? 0) >= (RANK[level] ?? 0),
              )
              .map(serverLine),
          );
        })
        .catch(fail);
    } else {
      api
        .askAgentLog(source, lines)
        .then(({ job_id }) => {
          const poll = () =>
            api
              .agentLog(job_id)
              .then((r) => {
                if (cancelled) return;
                if (r.status === "succeeded") done((r.text ?? "").split("\n"));
                else if (r.status === "failed" || r.status === "cancelled")
                  fail(new Error(r.error ?? t("logs.failed")));
                else timer = window.setTimeout(poll, 1500);
              })
              .catch(fail);
          poll();
        })
        .catch(fail);
    }
    return () => {
      cancelled = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [source, lines, level, tick, server, canManageSettings, t]);

  useEffect(() => {
    if (!auto || !server) return;
    const id = window.setInterval(() => setTick((n) => n + 1), 10000);
    return () => window.clearInterval(id);
  }, [auto, server]);

  const shown = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    return (text ?? []).filter(
      (l) => !needle || l.toLowerCase().includes(needle),
    );
  }, [text, filter]);

  if (!canManageSettings) {
    return (
      <p role="alert" className="alert error">
        {t("settings.forbidden")}
      </p>
    );
  }

  function download() {
    const blob = new Blob([shown.join("\n")], { type: "text/plain" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `carborewind-${source}-${new Date().toISOString().slice(0, 19)}.log`;
    a.click();
    URL.revokeObjectURL(url);
  }

  return (
    <>
      <h1>{t("logs.title")}</h1>
      <p className="muted">{t("logs.intro")}</p>
      <div className="toolbar">
        <label>
          {t("logs.source")}
          <select
            value={source}
            onChange={(e) => {
              setText(null);
              setSource(e.target.value as Source);
            }}
          >
            {SOURCES.map((s) => (
              <option key={s} value={s}>
                {t(`logs.sources.${s}`)}
              </option>
            ))}
          </select>
        </label>
        <label>
          {t("logs.lines")}
          <select
            value={lines}
            onChange={(e) => setLines(Number(e.target.value))}
          >
            {SIZES.map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        </label>
        {server && (
          <label>
            {t("logs.level")}
            <select
              value={level}
              onChange={(e) =>
                setLevel(e.target.value as (typeof LEVELS)[number])
              }
            >
              {LEVELS.map((l) => (
                <option key={l} value={l}>
                  {l ? t(`logs.levels.${l}`) : t("logs.levels.all")}
                </option>
              ))}
            </select>
          </label>
        )}
        <label className="grow">
          {t("logs.filter")}
          <input
            type="search"
            value={filter}
            placeholder={t("logs.filterPlaceholder")}
            onChange={(e) => setFilter(e.target.value)}
          />
        </label>
        <button
          type="button"
          className="secondary"
          disabled={state === "loading"}
          onClick={() => setTick((n) => n + 1)}
        >
          {t("logs.refresh")}
        </button>
        <button
          type="button"
          className="secondary"
          disabled={!shown.length}
          onClick={download}
        >
          {t("logs.download")}
        </button>
      </div>
      {server && (
        <label className="inline">
          <input
            type="checkbox"
            checked={auto}
            onChange={(e) => setAuto(e.target.checked)}
          />
          {t("logs.auto")}
        </label>
      )}
      <p className="muted small">{t(`logs.help.${source}`)}</p>
      {state === "loading" && <p className="muted">{t("logs.loading")}</p>}
      {state === "error" && error && (
        <p role="alert" className="alert error">
          {t("error", { message: error })}
        </p>
      )}
      {text && (
        <>
          <p className="muted small">
            {t("logs.count", { count: shown.length, total: text.length })}
          </p>
          <pre className="log-view" aria-label={t("logs.title")}>
            {shown.join("\n") || t("logs.empty")}
          </pre>
        </>
      )}
    </>
  );
}
