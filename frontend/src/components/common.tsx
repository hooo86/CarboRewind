import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { ApiError } from "../api/client";

export type Load<T> =
  | { state: "loading" }
  | { state: "error"; message: string }
  | { state: "ready"; data: T };

/**
 * Run `fetcher` on mount and whenever `deps` change; optionally poll every
 * `intervalMs`. Polling refreshes keep the previous data visible.
 */
export function useLoad<T>(
  fetcher: () => Promise<T>,
  deps: readonly unknown[],
  intervalMs?: number,
): [Load<T>, () => void] {
  const [load, setLoad] = useState<Load<T>>({ state: "loading" });
  const [tick, setTick] = useState(0);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;
  const lastTick = useRef(tick);

  useEffect(() => {
    let cancelled = false;
    const run = (initial: boolean) => {
      if (initial) setLoad({ state: "loading" });
      fetcherRef
        .current()
        .then((data) => !cancelled && setLoad({ state: "ready", data }))
        .catch((err: unknown) => {
          if (!cancelled)
            setLoad({ state: "error", message: errorMessage(err) });
        });
    };
    // A manual reload keeps the current data on screen; new deps show "loading".
    const isReload = lastTick.current !== tick;
    lastTick.current = tick;
    run(!isReload);
    const timer = intervalMs
      ? window.setInterval(() => run(false), intervalMs)
      : undefined;
    return () => {
      cancelled = true;
      if (timer) window.clearInterval(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick, intervalMs]);

  const reload = useCallback(() => setTick((n) => n + 1), []);
  return [load, reload];
}

export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.detail;
  if (err instanceof Error) return err.message;
  return String(err);
}

export function LoadView<T>({
  load,
  reload,
  children,
}: {
  load: Load<T>;
  reload?: () => void;
  children: (data: T) => React.ReactNode;
}) {
  const { t } = useTranslation();
  if (load.state === "loading") return <p className="muted">{t("loading")}</p>;
  if (load.state === "error") {
    return (
      <p role="alert" className="alert error">
        {t("error", { message: load.message })}{" "}
        {reload && (
          <button type="button" className="link" onClick={reload}>
            {t("retry")}
          </button>
        )}
      </p>
    );
  }
  return <>{children(load.data)}</>;
}

const TONE: Record<string, string> = {
  complete: "ok",
  succeeded: "ok",
  active: "ok",
  partial: "warn",
  queued: "info",
  running: "info",
  locked: "warn",
  failed: "bad",
  cancelled: "muted",
  expired: "muted",
  closed: "muted",
  deleted: "bad",
  disabled: "muted",
  enabled: "ok",
};

export function StatusBadge({ status }: { status: string | null | undefined }) {
  const { t } = useTranslation();
  if (!status) return <span className="muted">—</span>;
  return (
    <span className={`badge ${TONE[status] ?? "muted"}`}>
      {t(`status.${status}`, { defaultValue: status })}
    </span>
  );
}
