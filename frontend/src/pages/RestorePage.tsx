import { useEffect, useMemo, useState } from "react";
import type { FormEvent } from "react";
import { Link, useParams } from "react-router-dom";
import { useTranslation } from "react-i18next";

import { api } from "../api/endpoints";
import type {
  AccountDetail,
  AccountRow,
  Job,
  Page,
  RestoreMode,
  Snapshot,
  SnapshotFolder,
  SnapshotItem,
} from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { RestoreSummaryText } from "../components/JobTable";
import {
  LoadView,
  StatusBadge,
  errorMessage,
  useLoad,
} from "../components/common";
import { formatBytes, formatDate } from "../utils/format";

type Scope = "account" | "folders" | "items";
type Target = "same" | "other";

function depth(path: string): number {
  return path.split("/").length - 2;
}

function FolderPicker({
  folders,
  selected,
  toggle,
}: {
  folders: SnapshotFolder[];
  selected: Set<number>;
  toggle: (id: number) => void;
}) {
  const { t } = useTranslation();
  if (folders.length === 0)
    return <p className="muted">{t("restore.noFolder")}</p>;
  return (
    <ul className="tree">
      {folders.map((f) => (
        <li key={f.id} style={{ paddingLeft: `${depth(f.path) * 1.25}rem` }}>
          <label className="check">
            <input
              type="checkbox"
              checked={selected.has(f.id)}
              onChange={() => toggle(f.id)}
            />
            <span>{f.path.split("/").pop()}</span>
            <span className="muted">
              {t(`itemKind.${f.view}`)} · {f.item_count}
            </span>
          </label>
        </li>
      ))}
    </ul>
  );
}

function ItemPicker({
  accountId,
  snapshotId,
  folders,
  selected,
  toggle,
}: {
  accountId: string;
  snapshotId: string;
  folders: SnapshotFolder[];
  selected: Set<number>;
  toggle: (id: number) => void;
}) {
  const { t } = useTranslation();
  const [draft, setDraft] = useState("");
  const [q, setQ] = useState("");
  const [folder, setFolder] = useState("");
  const [offset, setOffset] = useState(0);
  const [items, reload] = useLoad<Page<SnapshotItem>>(
    () =>
      api.snapshotItems(accountId, snapshotId, {
        q: q || undefined,
        folder_id: folder ? Number(folder) : undefined,
        offset,
      }),
    [accountId, snapshotId, q, folder, offset],
  );
  const paths = useMemo(
    () => new Map(folders.map((f) => [f.id, f.path])),
    [folders],
  );

  return (
    <>
      <div className="toolbar">
        <label>
          {t("restore.folder")}
          <select
            value={folder}
            onChange={(e) => {
              setFolder(e.target.value);
              setOffset(0);
            }}
          >
            <option value="">{t("restore.allFolders")}</option>
            {folders.map((f) => (
              <option key={f.id} value={f.id}>
                {f.path}
              </option>
            ))}
          </select>
        </label>
        <label className="grow">
          {t("restore.search")}
          <input
            type="search"
            value={draft}
            placeholder={t("restore.searchPlaceholder")}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                setQ(draft.trim());
                setOffset(0);
              }
            }}
          />
        </label>
        <button
          type="button"
          className="secondary"
          onClick={() => {
            setQ(draft.trim());
            setOffset(0);
          }}
        >
          {t("restore.search")}
        </button>
      </div>
      <LoadView load={items} reload={reload}>
        {(page) =>
          page.items.length === 0 ? (
            <p className="muted">{t("restore.noItem")}</p>
          ) : (
            <>
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>
                        <span className="sr-only">{t("restore.select")}</span>
                      </th>
                      <th>{t("restore.subject")}</th>
                      <th>{t("restore.sender")}</th>
                      <th>{t("restore.folder")}</th>
                      <th>{t("restore.date")}</th>
                      <th className="num">{t("restore.size")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {page.items.map((i) => (
                      <tr key={i.id}>
                        <td>
                          <input
                            type="checkbox"
                            aria-label={t("restore.selectItem", {
                              subject: i.subject ?? i.id,
                            })}
                            checked={selected.has(i.id)}
                            onChange={() => toggle(i.id)}
                          />
                        </td>
                        <td className="wrap">
                          {i.subject ?? (
                            <span className="muted">
                              {t(`itemKind.${i.kind}`)} #{i.id}
                            </span>
                          )}
                        </td>
                        <td className="wrap">{i.sender ?? "—"}</td>
                        <td>{paths.get(i.folder_id) ?? i.folder_id}</td>
                        <td>{formatDate(i.date)}</td>
                        <td className="num">{formatBytes(i.size)}</td>
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

function TargetPicker({
  domainId,
  value,
  onChange,
}: {
  domainId: string;
  value: AccountRow | null;
  onChange: (a: AccountRow | null) => void;
}) {
  const { t } = useTranslation();
  const [q, setQ] = useState("");
  const [matches, setMatches] = useState<AccountRow[]>([]);
  useEffect(() => {
    if (q.trim().length < 2) {
      setMatches([]);
      return;
    }
    let cancelled = false;
    const timer = window.setTimeout(() => {
      api
        .accounts(domainId, q.trim())
        .then((page) => !cancelled && setMatches(page.items.slice(0, 8)))
        .catch(() => !cancelled && setMatches([]));
    }, 250);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [q, domainId]);

  if (value)
    return (
      <p>
        <strong>{value.email}</strong>{" "}
        <button type="button" className="link" onClick={() => onChange(null)}>
          {t("restore.change")}
        </button>
      </p>
    );
  return (
    <div>
      <label>
        {t("restore.targetSearch")}
        <input
          type="search"
          value={q}
          placeholder={t("accounts.searchPlaceholder")}
          onChange={(e) => setQ(e.target.value)}
        />
      </label>
      {matches.length > 0 && (
        <ul className="suggest">
          {matches.map((a) => (
            <li key={a.id}>
              <button
                type="button"
                className="link"
                onClick={() => onChange(a)}
              >
                {a.email}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function JobFollow({ jobId }: { jobId: string }) {
  const { t } = useTranslation();
  const [job, setJob] = useState<Job | null>(null);
  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;
    const poll = () => {
      api
        .job(jobId)
        .then((j) => {
          if (cancelled) return;
          setJob(j);
          if (j.status === "queued" || j.status === "running")
            timer = window.setTimeout(poll, 3000);
        })
        .catch(() => {
          if (!cancelled) timer = window.setTimeout(poll, 5000);
        });
    };
    poll();
    return () => {
      cancelled = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [jobId]);
  if (!job) return null;
  return (
    <div className="card result" aria-live="polite">
      <p>
        {job.dry_run ? t("restore.simulationTitle") : t("restore.restoration")}{" "}
        <StatusBadge status={job.status} />
      </p>
      {job.status === "succeeded" && <RestoreSummaryText job={job} />}
      {job.error && <p className="alert error">{job.error}</p>}
      {(job.status === "queued" || job.status === "running") && (
        <p className="muted">{t("restore.waiting")}</p>
      )}
    </div>
  );
}

export function RestorePage() {
  const { id = "", snapshotId = "" } = useParams();
  const { t } = useTranslation();
  const { canRestore } = useAuth();
  const [account] = useLoad<AccountDetail>(() => api.account(id), [id]);
  const [snapshots] = useLoad<Page<Snapshot>>(() => api.snapshots(id), [id]);
  const [folders, reloadFolders] = useLoad<SnapshotFolder[]>(
    () => api.snapshotFolders(id, snapshotId),
    [id, snapshotId],
  );
  const [scope, setScope] = useState<Scope>("account");
  const [pickedFolders, setPickedFolders] = useState<Set<number>>(new Set());
  const [pickedItems, setPickedItems] = useState<Set<number>>(new Set());
  const [target, setTarget] = useState<Target>("same");
  const [other, setOther] = useState<AccountRow | null>(null);
  const [mode, setMode] = useState<RestoreMode>("new_folder");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [jobs, setJobs] = useState<string[]>([]);

  const toggle =
    (setter: typeof setPickedFolders) =>
    (value: number): void =>
      setter((prev) => {
        const next = new Set(prev);
        if (next.has(value)) next.delete(value);
        else next.add(value);
        return next;
      });

  const snapshot =
    snapshots.state === "ready"
      ? snapshots.data.items.find((s) => s.id === snapshotId)
      : undefined;
  const effectiveMode: RestoreMode = target === "other" ? "new_folder" : mode;
  const selectionMissing =
    (scope === "folders" && pickedFolders.size === 0) ||
    (scope === "items" && pickedItems.size === 0);
  const targetMissing = target === "other" && !other;

  async function submit(dryRun: boolean, event?: FormEvent) {
    event?.preventDefault();
    if (!dryRun && !window.confirm(t("restore.confirm"))) return;
    setBusy(true);
    setError(null);
    try {
      const job = await api.startRestore({
        account_id: id,
        snapshot_id: snapshotId,
        target_account_id: target === "other" ? other?.id : undefined,
        mode: effectiveMode,
        dry_run: dryRun,
        folder_ids: scope === "folders" ? [...pickedFolders] : undefined,
        item_ids: scope === "items" ? [...pickedItems] : undefined,
      });
      setJobs((prev) => [job.id, ...prev]);
    } catch (err) {
      setError(t("error", { message: errorMessage(err) }));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <p>
        <Link to={`/accounts/${id}`}>{t("restore.back")}</Link>
      </p>
      <LoadView load={account}>
        {(a) =>
          !canRestore(a.domain_id) ? (
            <p role="alert" className="alert error">
              {t("restore.forbidden")}
            </p>
          ) : (
            <form onSubmit={(e) => void submit(false, e)}>
              <h1>{t("restore.title", { email: a.email })}</h1>
              {snapshot && (
                <p className="muted">
                  {t("restore.snapshot", {
                    seq: snapshot.seq,
                    date: formatDate(snapshot.taken_at),
                    items: snapshot.item_count,
                  })}
                </p>
              )}

              <fieldset>
                <legend>{t("restore.what")}</legend>
                {(["account", "folders", "items"] as const).map((s) => (
                  <label key={s} className="check">
                    <input
                      type="radio"
                      name="scope"
                      checked={scope === s}
                      onChange={() => setScope(s)}
                    />
                    {t(`restore.scope.${s}`)}
                  </label>
                ))}
                {scope === "folders" && (
                  <LoadView load={folders} reload={reloadFolders}>
                    {(list) => (
                      <FolderPicker
                        folders={list}
                        selected={pickedFolders}
                        toggle={toggle(setPickedFolders)}
                      />
                    )}
                  </LoadView>
                )}
                {scope === "items" && (
                  <LoadView load={folders} reload={reloadFolders}>
                    {(list) => (
                      <ItemPicker
                        accountId={id}
                        snapshotId={snapshotId}
                        folders={list}
                        selected={pickedItems}
                        toggle={toggle(setPickedItems)}
                      />
                    )}
                  </LoadView>
                )}
                {scope !== "account" && (
                  <p className="muted">
                    {t("restore.selected", {
                      count:
                        scope === "folders"
                          ? pickedFolders.size
                          : pickedItems.size,
                    })}
                  </p>
                )}
              </fieldset>

              <fieldset>
                <legend>{t("restore.where")}</legend>
                <label className="check">
                  <input
                    type="radio"
                    name="target"
                    checked={target === "same"}
                    onChange={() => setTarget("same")}
                  />
                  {t("restore.sameAccount", { email: a.email })}
                </label>
                <label className="check">
                  <input
                    type="radio"
                    name="target"
                    checked={target === "other"}
                    onChange={() => setTarget("other")}
                  />
                  {t("restore.otherAccount")}
                </label>
                {target === "other" && (
                  <TargetPicker
                    domainId={a.domain_id}
                    value={other}
                    onChange={setOther}
                  />
                )}
                <label className="check">
                  <input
                    type="radio"
                    name="mode"
                    checked={effectiveMode === "new_folder"}
                    onChange={() => setMode("new_folder")}
                  />
                  <span>
                    {t("restore.mode.new_folder")}
                    <span className="hint">
                      {t("restore.mode.new_folder_hint")}
                    </span>
                  </span>
                </label>
                <label className="check">
                  <input
                    type="radio"
                    name="mode"
                    disabled={target === "other"}
                    checked={effectiveMode === "original"}
                    onChange={() => setMode("original")}
                  />
                  <span>
                    {t("restore.mode.original")}
                    <span className="hint">
                      {t("restore.mode.original_hint")}
                    </span>
                  </span>
                </label>
              </fieldset>

              {error && (
                <p role="alert" className="alert error">
                  {error}
                </p>
              )}
              <div className="actions">
                <button
                  type="button"
                  className="secondary"
                  disabled={busy || selectionMissing || targetMissing}
                  onClick={() => void submit(true)}
                >
                  {t("restore.simulate")}
                </button>
                <button
                  type="submit"
                  disabled={busy || selectionMissing || targetMissing}
                >
                  {t("restore.submit")}
                </button>
              </div>
              {jobs.map((jobId) => (
                <JobFollow key={jobId} jobId={jobId} />
              ))}
            </form>
          )
        }
      </LoadView>
    </>
  );
}
