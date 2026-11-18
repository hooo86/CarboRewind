import { useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { useTranslation } from "react-i18next";

import { api } from "../api/endpoints";
import type {
  AccountFacets,
  AccountItem,
  BackupType,
  FieldType,
  Page,
} from "../api/types";
import { useAuth } from "../auth/AuthContext";
import {
  LoadView,
  StatusBadge,
  errorMessage,
  useLoad,
} from "../components/common";
import { NoticeBar } from "../components/notices";
import type { Notice } from "../components/notices";
import { formatBytes, formatDate, formatRelative } from "../utils/format";

export { NoticeBar, useBackupNotices } from "../components/notices";

const PAGE_SIZES = [25, 50, 100, 200];
const COLUMNS_KEY = "carborewind.accounts.columns";

type Column = {
  key: string;
  sort?: string;
  render: (
    a: AccountItem,
    t: (k: string, o?: Record<string, unknown>) => string,
  ) => ReactNode;
  num?: boolean;
};

const dash = <span className="muted">—</span>;

export const COLUMNS: Column[] = [
  {
    key: "email",
    sort: "email",
    render: (a) => <Link to={`/accounts/${a.id}`}>{a.email}</Link>,
  },
  {
    key: "display_name",
    sort: "display_name",
    render: (a) => a.display_name ?? dash,
  },
  {
    key: "first_name",
    sort: "first_name",
    render: (a) => a.first_name ?? dash,
  },
  { key: "last_name", sort: "last_name", render: (a) => a.last_name ?? dash },
  { key: "domain", sort: "domain", render: (a) => a.domain },
  {
    key: "aliases",
    render: (a) => (a.aliases.length ? a.aliases.join(", ") : dash),
  },
  {
    key: "status",
    sort: "status",
    render: (a) => <StatusBadge status={a.status} />,
  },
  { key: "cos", sort: "cos", render: (a) => a.cos ?? dash },
  {
    key: "is_admin",
    sort: "is_admin",
    render: (a, t) => (a.is_admin ? t("yes") : ""),
  },
  {
    key: "quota_bytes",
    sort: "quota_bytes",
    num: true,
    render: (a, t) =>
      a.quota_bytes ? (
        formatBytes(a.quota_bytes)
      ) : (
        <span className="muted">{t("accounts.unlimited")}</span>
      ),
  },
  {
    key: "used_bytes",
    sort: "used_bytes",
    num: true,
    render: (a) => (a.used_bytes !== null ? formatBytes(a.used_bytes) : dash),
  },
  {
    key: "usage_pct",
    sort: "usage_pct",
    num: true,
    render: (a) =>
      a.usage_pct !== null ? (
        <span
          className={
            a.usage_pct >= 90
              ? "bad-text"
              : a.usage_pct >= 80
                ? "warn-text"
                : ""
          }
        >
          {a.usage_pct.toLocaleString()} %
        </span>
      ) : (
        dash
      ),
  },
  {
    key: "last_login_at",
    sort: "last_login_at",
    render: (a) => formatDate(a.last_login_at),
  },
  {
    key: "carbonio_created_at",
    sort: "carbonio_created_at",
    render: (a) => formatDate(a.carbonio_created_at),
  },
  {
    key: "last_seen_at",
    sort: "last_seen_at",
    render: (a) => formatDate(a.last_seen_at),
  },
  {
    key: "last_backup_at",
    sort: "last_backup_at",
    render: (a, t) =>
      a.last_backup_at ? (
        <span title={formatDate(a.last_backup_at)}>
          {formatRelative(a.last_backup_at)}{" "}
          <StatusBadge status={a.last_backup_status} />
        </span>
      ) : (
        <span className="muted">{t("accounts.never")}</span>
      ),
  },
  {
    key: "last_backup_type",
    sort: "last_backup_type",
    render: (a, t) =>
      a.last_backup_type ? t(`backupType.${a.last_backup_type}`) : dash,
  },
  {
    key: "snapshot_count",
    sort: "snapshot_count",
    num: true,
    render: (a) => a.snapshot_count,
  },
  {
    key: "stored_bytes",
    sort: "stored_bytes",
    num: true,
    render: (a) =>
      a.stored_bytes !== null ? formatBytes(a.stored_bytes) : dash,
  },
  {
    key: "policies",
    sort: "policy",
    render: (a, t) =>
      a.policies.length ? (
        a.policies.join(", ")
      ) : (
        <span className="warn-text">{t("accounts.noPolicy")}</span>
      ),
  },
];

const DEFAULT_COLUMNS = [
  "email",
  "display_name",
  "status",
  "usage_pct",
  "last_backup_at",
  "policies",
];

/** Ready-made filters, toggled from the "Filters" menu (Odoo-style). */
export const PRESETS: { key: string; filters: string[] }[] = [
  { key: "never", filters: ["last_backup_at:not_set"] },
  { key: "stale", filters: ["last_backup_at:older_days:1"] },
  { key: "partial", filters: ["last_backup_status:in:partial"] },
  { key: "noPolicy", filters: ["has_policy:is:false"] },
  { key: "quota80", filters: ["usage_pct:ge:80"] },
  { key: "admins", filters: ["is_admin:is:true"] },
  { key: "inactive", filters: ["last_login_at:older_days:90"] },
  { key: "locked", filters: ["status:in:locked,closed"] },
  { key: "deleted", filters: ["status:in:deleted"] },
];

const OPERATORS: Record<FieldType, string[]> = {
  text: ["contains", "not_contains", "eq", "ne", "set", "not_set"],
  number: ["gt", "ge", "lt", "le", "eq", "ne", "set", "not_set"],
  date: ["older_days", "within_days", "before", "after", "set", "not_set"],
  choice: ["in", "not_in", "set", "not_set"],
  boolean: ["is"],
};

const NO_VALUE = new Set(["set", "not_set"]);
const CHOICES: Record<string, string[]> = {
  status: ["active", "locked", "closed", "deleted"],
  last_backup_status: ["complete", "partial", "expired"],
  last_backup_type: ["full", "incremental"],
};

function loadColumns(): string[] {
  try {
    const saved = JSON.parse(
      localStorage.getItem(COLUMNS_KEY) ?? "null",
    ) as unknown;
    if (Array.isArray(saved) && saved.every((c) => typeof c === "string")) {
      const known = saved.filter((c) => COLUMNS.some((col) => col.key === c));
      if (known.length) return known;
    }
  } catch {
    /* storage unavailable or corrupted */
  }
  return DEFAULT_COLUMNS;
}

function saveColumns(columns: string[]): void {
  try {
    localStorage.setItem(COLUMNS_KEY, JSON.stringify(columns));
  } catch {
    /* storage unavailable */
  }
}

/** Human label of one filter, e.g. "Usage ≥ 80". */
export function filterLabel(
  raw: string,
  t: (k: string, o?: Record<string, unknown>) => string,
): string {
  const preset = PRESETS.find(
    (p) => p.filters.length === 1 && p.filters[0] === raw,
  );
  if (preset) return t(`filters.presets.${preset.key}`);
  const [field = "", op = "", ...rest] = raw.split(":");
  const value = rest.join(":");
  const shown =
    field in CHOICES || field === "cos"
      ? value
          .split(",")
          .map((v) =>
            t(`status.${v}`, {
              defaultValue: t(`backupType.${v}`, { defaultValue: v }),
            }),
          )
          .join(", ")
      : value === "true"
        ? t("yes")
        : value === "false"
          ? t("no")
          : value;
  return `${t(`columns.${field === "policy" ? "policies" : field}`, { defaultValue: field })} ${t(`filters.ops.${op}`)}${
    NO_VALUE.has(op) ? "" : ` ${shown}`
  }`;
}

function Dropdown({
  label,
  children,
}: {
  label: string;
  children: (close: () => void) => ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node))
        setOpen(false);
    };
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, [open]);
  return (
    <div className="dropdown" ref={ref}>
      <button
        type="button"
        className="secondary"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        {label} ▾
      </button>
      {open && (
        <div className="dropdown-panel" role="dialog" aria-label={label}>
          {children(() => setOpen(false))}
        </div>
      )}
    </div>
  );
}

function CustomFilter({
  facets,
  onAdd,
}: {
  facets: AccountFacets;
  onAdd: (filter: string) => void;
}) {
  const { t } = useTranslation();
  const fields = Object.keys(facets.fields);
  const [field, setField] = useState(fields[0] ?? "email");
  const type = facets.fields[field] ?? "text";
  const [op, setOp] = useState(OPERATORS[type][0] ?? "contains");
  const [value, setValue] = useState("");
  const [picked, setPicked] = useState<string[]>([]);
  const choices =
    field === "cos"
      ? facets.cos
      : field === "domain"
        ? facets.domains.map((d) => d.name)
        : CHOICES[field];

  useEffect(() => {
    setOp(OPERATORS[facets.fields[field] ?? "text"][0] ?? "eq");
    setValue(type === "boolean" ? "true" : "");
    setPicked([]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [field]);

  const needsValue = !NO_VALUE.has(op);
  const finalValue = type === "choice" ? picked.join(",") : value.trim();
  const valid = !needsValue || finalValue !== "";

  return (
    <form
      className="custom-filter"
      onSubmit={(e) => {
        e.preventDefault();
        if (valid)
          onAdd(needsValue ? `${field}:${op}:${finalValue}` : `${field}:${op}`);
      }}
    >
      <label>
        {t("filters.field")}
        <select value={field} onChange={(e) => setField(e.target.value)}>
          {fields.map((f) => (
            <option key={f} value={f}>
              {t(`columns.${f === "policy" ? "policies" : f}`, {
                defaultValue: f,
              })}
            </option>
          ))}
        </select>
      </label>
      <label>
        {t("filters.operator")}
        <select value={op} onChange={(e) => setOp(e.target.value)}>
          {OPERATORS[type].map((o) => (
            <option key={o} value={o}>
              {t(`filters.ops.${o}`)}
            </option>
          ))}
        </select>
      </label>
      {needsValue && type === "boolean" && (
        <label>
          {t("filters.value")}
          <select value={value} onChange={(e) => setValue(e.target.value)}>
            <option value="true">{t("yes")}</option>
            <option value="false">{t("no")}</option>
          </select>
        </label>
      )}
      {needsValue && type === "choice" && (
        <fieldset className="choices">
          <legend>{t("filters.value")}</legend>
          {(choices ?? []).map((c) => (
            <label key={c} className="check">
              <input
                type="checkbox"
                checked={picked.includes(c)}
                onChange={() =>
                  setPicked((p) =>
                    p.includes(c) ? p.filter((x) => x !== c) : [...p, c],
                  )
                }
              />
              {t(`status.${c}`, {
                defaultValue: t(`backupType.${c}`, { defaultValue: c }),
              })}
            </label>
          ))}
        </fieldset>
      )}
      {needsValue &&
        (type === "text" || type === "number" || type === "date") && (
          <label>
            {t("filters.value")}
            {type === "text" && field === "domain" ? (
              <select value={value} onChange={(e) => setValue(e.target.value)}>
                <option value="" />
                {(choices ?? []).map((c) => (
                  <option key={c} value={c}>
                    {c}
                  </option>
                ))}
              </select>
            ) : (
              <input
                type={
                  type === "number" ||
                  op === "older_days" ||
                  op === "within_days"
                    ? "number"
                    : type === "date"
                      ? "date"
                      : "text"
                }
                value={value}
                onChange={(e) => setValue(e.target.value)}
                placeholder={
                  op === "older_days" || op === "within_days"
                    ? t("filters.days")
                    : undefined
                }
              />
            )}
          </label>
        )}
      <button type="submit" disabled={!valid}>
        {t("filters.apply")}
      </button>
    </form>
  );
}

export function AccountsPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { canRunBackup, canManagePolicies } = useAuth();
  const [params, setParams] = useSearchParams();
  const filters = params.getAll("f");
  const q = params.get("q") ?? "";
  const sort = params.get("sort") ?? "email";
  const order = params.get("order") === "desc" ? "desc" : "asc";
  const limit = Number(params.get("limit")) || 50;
  const offset = Number(params.get("offset")) || 0;
  const [draft, setDraft] = useState(q);
  const [columns, setColumns] = useState<string[]>(loadColumns);
  const [selected, setSelected] = useState<Map<string, AccountItem>>(new Map());
  const [notice, setNotice] = useState<Notice>(null);
  const [busy, setBusy] = useState(false);

  const [facets] = useLoad<AccountFacets>(() => api.accountFacets(), []);
  const key = params.toString();
  const [accounts, reload] = useLoad<Page<AccountItem>>(
    () => api.accountList({ q, filters, sort, order, limit, offset }),
    [key],
  );

  const update = (
    changes: Record<string, string | string[] | null>,
    keepOffset = false,
  ) => {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(changes)) {
      next.delete(k);
      if (Array.isArray(v)) v.forEach((x) => next.append(k, x));
      else if (v) next.set(k, v);
    }
    if (!keepOffset) next.delete("offset");
    setParams(next, { replace: true });
  };
  const toggleFilter = (filter: string) =>
    update({
      f: filters.includes(filter)
        ? filters.filter((f) => f !== filter)
        : [...filters, filter],
    });
  const setSort = (field: string) =>
    update({
      sort: field,
      order: sort === field && order === "asc" ? "desc" : "asc",
    });

  const visible = COLUMNS.filter((c) => columns.includes(c.key));
  const pageItems = accounts.state === "ready" ? accounts.data.items : [];
  const allPageSelected =
    pageItems.length > 0 && pageItems.every((a) => selected.has(a.id));
  const selection = [...selected.values()];
  const mayBackup =
    selection.length > 0 && selection.every((a) => canRunBackup(a.domain_id));
  const mayPolicy =
    selection.length > 0 &&
    selection.every((a) => canManagePolicies(a.domain_id));

  const toggleOne = (a: AccountItem) =>
    setSelected((prev) => {
      const next = new Map(prev);
      if (next.has(a.id)) next.delete(a.id);
      else next.set(a.id, a);
      return next;
    });
  const togglePage = () =>
    setSelected((prev) => {
      const next = new Map(prev);
      if (allPageSelected) pageItems.forEach((a) => next.delete(a.id));
      else pageItems.forEach((a) => next.set(a.id, a));
      return next;
    });

  async function bulkBackup(type: BackupType) {
    setBusy(true);
    try {
      const r = await api.bulkBackup([...selected.keys()], type);
      setNotice({
        kind: "ok",
        text: t("accounts.bulkQueued", {
          count: r.queued,
          type: t(`backupType.${type}`),
          busy: r.skipped_busy,
          deleted: r.skipped_deleted,
        }),
      });
      setSelected(new Map());
      reload();
    } catch (err) {
      setNotice({
        kind: "error",
        text: t("error", { message: errorMessage(err) }),
      });
    } finally {
      setBusy(false);
    }
  }

  const presetActive = (filtersOfPreset: string[]) =>
    filtersOfPreset.every((f) => filters.includes(f));

  const columnLabel = (key: string) => t(`columns.${key}`);
  const sortIndicator = (field?: string) =>
    field && sort === field ? (order === "asc" ? " ▲" : " ▼") : "";

  const totalLabel = useMemo(() => {
    if (accounts.state !== "ready") return "";
    const p = accounts.data;
    return p.total === 0
      ? ""
      : t("accounts.count", {
          shown: `${p.offset + 1}–${p.offset + p.items.length}`,
          total: p.total,
        });
  }, [accounts, t]);

  return (
    <>
      <h1>{t("accounts.title")}</h1>
      <div className="searchbar">
        <form
          role="search"
          className="grow search-form"
          onSubmit={(e) => {
            e.preventDefault();
            update({ q: draft.trim() || null });
          }}
        >
          <div className="chips-input">
            {filters.map((f) => (
              <span key={f} className="chip">
                {filterLabel(f, t)}
                <button
                  type="button"
                  className="chip-x"
                  aria-label={t("filters.remove", {
                    filter: filterLabel(f, t),
                  })}
                  onClick={() => toggleFilter(f)}
                >
                  ×
                </button>
              </span>
            ))}
            <input
              type="search"
              aria-label={t("accounts.search")}
              value={draft}
              placeholder={t("accounts.searchPlaceholder")}
              onChange={(e) => setDraft(e.target.value)}
            />
          </div>
        </form>
        <Dropdown label={t("filters.title")}>
          {(close) => (
            <div className="filter-menu">
              <ul className="menu">
                {PRESETS.map((p) => (
                  <li key={p.key}>
                    <label className="check">
                      <input
                        type="checkbox"
                        checked={presetActive(p.filters)}
                        onChange={() => {
                          update({
                            f: presetActive(p.filters)
                              ? filters.filter((f) => !p.filters.includes(f))
                              : [
                                  ...filters,
                                  ...p.filters.filter(
                                    (f) => !filters.includes(f),
                                  ),
                                ],
                          });
                        }}
                      />
                      {t(`filters.presets.${p.key}`)}
                    </label>
                  </li>
                ))}
              </ul>
              <h3>{t("filters.custom")}</h3>
              {facets.state === "ready" ? (
                <CustomFilter
                  facets={facets.data}
                  onAdd={(f) => {
                    if (!filters.includes(f)) update({ f: [...filters, f] });
                    close();
                  }}
                />
              ) : (
                <p className="muted">{t("loading")}</p>
              )}
              {filters.length > 0 && (
                <button
                  type="button"
                  className="link"
                  onClick={() => update({ f: [] })}
                >
                  {t("filters.clear")}
                </button>
              )}
            </div>
          )}
        </Dropdown>
        <Dropdown label={t("columns.title")}>
          {() => (
            <ul className="menu columns-menu">
              {COLUMNS.map((c) => (
                <li key={c.key}>
                  <label className="check">
                    <input
                      type="checkbox"
                      disabled={c.key === "email"}
                      checked={columns.includes(c.key)}
                      onChange={() => {
                        const next = columns.includes(c.key)
                          ? columns.filter((k) => k !== c.key)
                          : COLUMNS.map((col) => col.key).filter(
                              (k) => k === c.key || columns.includes(k),
                            );
                        setColumns(next);
                        saveColumns(next);
                      }}
                    />
                    {columnLabel(c.key)}
                  </label>
                </li>
              ))}
              <li>
                <button
                  type="button"
                  className="link"
                  onClick={() => {
                    setColumns(DEFAULT_COLUMNS);
                    saveColumns(DEFAULT_COLUMNS);
                  }}
                >
                  {t("columns.reset")}
                </button>
              </li>
            </ul>
          )}
        </Dropdown>
      </div>

      {selected.size > 0 && (
        <div
          className="selection-bar"
          role="region"
          aria-label={t("accounts.selection")}
        >
          <strong>{t("accounts.selected", { count: selected.size })}</strong>
          {mayBackup && (
            <>
              <button
                type="button"
                className="small"
                disabled={busy}
                onClick={() => void bulkBackup("incremental")}
              >
                {t("accounts.bulkIncremental")}
              </button>
              <button
                type="button"
                className="small secondary"
                disabled={busy}
                onClick={() => void bulkBackup("full")}
              >
                {t("accounts.bulkFull")}
              </button>
            </>
          )}
          {mayPolicy && (
            <button
              type="button"
              className="small secondary"
              onClick={() =>
                navigate("/policies", {
                  state: {
                    accountIds: [...selected.keys()],
                    emails: selection.map((a) => a.email),
                  },
                })
              }
            >
              {t("accounts.createPolicy")}
            </button>
          )}
          <button
            type="button"
            className="link"
            onClick={() => setSelected(new Map())}
          >
            {t("accounts.clearSelection")}
          </button>
        </div>
      )}
      <NoticeBar notice={notice} />

      <LoadView load={accounts} reload={reload}>
        {(page) =>
          page.items.length === 0 ? (
            <p className="muted">{t("accounts.empty")}</p>
          ) : (
            <>
              <div className="table-wrap">
                <table className="accounts">
                  <thead>
                    <tr>
                      <th>
                        <input
                          type="checkbox"
                          aria-label={t("accounts.selectPage")}
                          checked={allPageSelected}
                          onChange={togglePage}
                        />
                      </th>
                      {visible.map((c) => (
                        <th key={c.key} className={c.num ? "num" : undefined}>
                          {c.sort ? (
                            <button
                              type="button"
                              className="th-sort"
                              onClick={() => setSort(c.sort as string)}
                            >
                              {columnLabel(c.key)}
                              {sortIndicator(c.sort)}
                            </button>
                          ) : (
                            columnLabel(c.key)
                          )}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {page.items.map((a) => (
                      <tr
                        key={a.id}
                        className={selected.has(a.id) ? "selected" : undefined}
                      >
                        <td>
                          <input
                            type="checkbox"
                            aria-label={t("accounts.selectOne", {
                              email: a.email,
                            })}
                            checked={selected.has(a.id)}
                            onChange={() => toggleOne(a)}
                          />
                        </td>
                        {visible.map((c) => (
                          <td key={c.key} className={c.num ? "num" : undefined}>
                            {c.render(a, t)}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="pager">
                <span className="muted">{totalLabel}</span>
                <label className="inline">
                  {t("accounts.pageSize")}
                  <select
                    value={limit}
                    onChange={(e) => update({ limit: e.target.value })}
                  >
                    {PAGE_SIZES.map((n) => (
                      <option key={n} value={n}>
                        {n}
                      </option>
                    ))}
                  </select>
                </label>
                <button
                  type="button"
                  className="secondary small"
                  disabled={page.offset === 0}
                  onClick={() =>
                    update(
                      { offset: String(Math.max(0, page.offset - page.limit)) },
                      true,
                    )
                  }
                >
                  {t("accounts.previous")}
                </button>
                <button
                  type="button"
                  className="secondary small"
                  disabled={page.offset + page.items.length >= page.total}
                  onClick={() =>
                    update({ offset: String(page.offset + page.limit) }, true)
                  }
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
