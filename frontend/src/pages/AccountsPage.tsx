import { useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { useTranslation } from "react-i18next";

import { api } from "../api/endpoints";
import type {
  AccountFacets,
  AccountGroup,
  AccountItem,
  BackupType,
  FieldType,
  Page,
  SavedView,
} from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { QuotaForm } from "../components/QuotaForm";
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

/** Carbonio profile fields (sent by the agent, filterable and sortable). */
export const PROFILE_FIELDS = [
  "title",
  "company",
  "department",
  "phone",
  "mobile",
  "home_phone",
  "street",
  "postal_code",
  "city",
  "state",
  "country",
  "description",
  "notes",
  "employee_number",
];

COLUMNS.push(
  ...PROFILE_FIELDS.map(
    (field): Column => ({
      key: field,
      sort: field,
      render: (a) => a.profile?.[field] ?? dash,
    }),
  ),
);

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

/** Filters selecting one group of ``GET /api/accounts/groups``. */
export function groupFilters(
  field: string,
  type: FieldType,
  value: string | null,
): string[] {
  if (value === null) return [`${field}:not_set`];
  if (type === "boolean") return [`${field}:is:${value}`];
  if (type === "choice") return [`${field}:in:${value}`];
  if (type === "date") {
    const [y = 1970, m = 1] = value.split("-").map(Number);
    const first = new Date(Date.UTC(y, m - 1, 1));
    const before = new Date(Date.UTC(y, m, 1));
    const dayBefore = new Date(first.getTime() - 86_400_000);
    const iso = (d: Date) => d.toISOString().slice(0, 10);
    return [
      `${field}:after:${iso(dayBefore)}`,
      `${field}:before:${iso(before)}`,
    ];
  }
  return [`${field}:eq:${value}`];
}

/** Human label of a group value. */
export function groupLabel(
  field: string,
  type: FieldType,
  value: string | null,
  t: (k: string, o?: Record<string, unknown>) => string,
): string {
  if (value === null) return t("groups.none");
  if (type === "boolean") return value === "true" ? t("yes") : t("no");
  if (type === "date") {
    const [y = 1970, m = 1] = value.split("-").map(Number);
    return new Date(Date.UTC(y, m - 1, 15)).toLocaleDateString(undefined, {
      month: "long",
      year: "numeric",
    });
  }
  if (field in CHOICES)
    return t(`status.${value}`, {
      defaultValue: t(`backupType.${value}`, { defaultValue: value }),
    });
  return value;
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

function AccountTable({
  items,
  visible,
  selected,
  onToggle,
  onTogglePage,
  sort,
  order,
  onSort,
}: {
  items: AccountItem[];
  visible: Column[];
  selected: Map<string, AccountItem>;
  onToggle: (a: AccountItem) => void;
  onTogglePage: (items: AccountItem[], select: boolean) => void;
  sort: string;
  order: "asc" | "desc";
  onSort: (field: string) => void;
}) {
  const { t } = useTranslation();
  const all = items.length > 0 && items.every((a) => selected.has(a.id));
  return (
    <div className="table-wrap">
      <table className="accounts">
        <thead>
          <tr>
            <th>
              <input
                type="checkbox"
                aria-label={t("accounts.selectPage")}
                checked={all}
                onChange={() => onTogglePage(items, !all)}
              />
            </th>
            {visible.map((c) => (
              <th key={c.key} className={c.num ? "num" : undefined}>
                {c.sort ? (
                  <button
                    type="button"
                    className="th-sort"
                    onClick={() => onSort(c.sort as string)}
                  >
                    {t(`columns.${c.key}`)}
                    {sort === c.sort ? (order === "asc" ? " ▲" : " ▼") : ""}
                  </button>
                ) : (
                  t(`columns.${c.key}`)
                )}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {items.map((a) => (
            <tr
              key={a.id}
              className={selected.has(a.id) ? "selected" : undefined}
            >
              <td>
                <input
                  type="checkbox"
                  aria-label={t("accounts.selectOne", { email: a.email })}
                  checked={selected.has(a.id)}
                  onChange={() => onToggle(a)}
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
  );
}

const GROUP_PAGE = 100;

/** One group of the grouped view: header with figures, accounts loaded on opening. */
function GroupSection({
  group,
  field,
  type,
  q,
  filters,
  sort,
  order,
  tableProps,
  onOpen,
}: {
  group: AccountGroup;
  field: string;
  type: FieldType;
  q: string;
  filters: string[];
  sort: string;
  order: "asc" | "desc";
  tableProps: Omit<
    Parameters<typeof AccountTable>[0],
    "items" | "sort" | "order"
  >;
  onOpen: (extra: string[]) => void;
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState<AccountItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const extra = groupFilters(field, type, group.value);
  const label = groupLabel(field, type, group.value, t);
  const key = [q, ...filters, ...extra, sort, order].join("|");

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setError(null);
    api
      .accountList({
        q: q || undefined,
        filters: [...filters, ...extra],
        sort,
        order,
        limit: GROUP_PAGE,
        offset: 0,
      })
      .then((p) => !cancelled && setItems(p.items))
      .catch((err: unknown) => !cancelled && setError(errorMessage(err)));
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, key]);

  return (
    <div className="group">
      <button
        type="button"
        className="group-head"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        <span className="group-caret">{open ? "▾" : "▸"}</span>
        <strong>{label}</strong>
        <span className="muted">
          {t("groups.count", { count: group.count })}
          {group.used_bytes > 0 &&
            ` · ${t("groups.used", { size: formatBytes(group.used_bytes) })}`}
          {group.stored_bytes > 0 &&
            ` · ${t("groups.stored", { size: formatBytes(group.stored_bytes) })}`}
        </span>
      </button>
      {open && (
        <div className="group-body">
          {error && (
            <p role="alert" className="alert error">
              {t("error", { message: error })}
            </p>
          )}
          {items === null && !error && <p className="muted">{t("loading")}</p>}
          {items && (
            <>
              <AccountTable
                {...tableProps}
                items={items}
                sort={sort}
                order={order}
              />
              {group.count > items.length && (
                <button
                  type="button"
                  className="link"
                  onClick={() => onOpen(extra)}
                >
                  {t("groups.openAll", { count: group.count })}
                </button>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}

/** Saved searches of the page (Odoo "Favourites"): apply, save, share, delete. */
function Favorites({
  page,
  current,
  onApply,
}: {
  page: string;
  current: string;
  onApply: (query: string) => void;
}) {
  const { t } = useTranslation();
  const [views, setViews] = useState<SavedView[] | null>(null);
  const [name, setName] = useState("");
  const [shared, setShared] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const load = () =>
    api
      .views(page)
      .then(setViews)
      .catch((err: unknown) => setError(errorMessage(err)));
  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page]);

  async function save() {
    setError(null);
    try {
      await api.saveView({ page, name, query: current, shared });
      setName("");
      setShared(false);
      await load();
    } catch (err) {
      setError(errorMessage(err));
    }
  }

  async function remove(view: SavedView) {
    if (!window.confirm(t("views.confirmDelete", { name: view.name }))) return;
    try {
      await api.deleteView(view.id);
      await load();
    } catch (err) {
      setError(errorMessage(err));
    }
  }

  return (
    <div className="favorites">
      {views === null && !error && <p className="muted">{t("loading")}</p>}
      {views && views.length === 0 && (
        <p className="muted small">{t("views.none")}</p>
      )}
      {views && views.length > 0 && (
        <ul className="menu">
          {views.map((v) => (
            <li key={v.id} className="view-item">
              <button
                type="button"
                className={`link${v.query === current ? " current" : ""}`}
                onClick={() => onApply(v.query)}
              >
                {v.name}
              </button>
              {v.shared && (
                <span className="muted small">
                  {v.mine
                    ? t("views.sharedByMe")
                    : t("views.sharedBy", { owner: v.owner })}
                </span>
              )}
              {v.mine && (
                <button
                  type="button"
                  className="chip-x"
                  aria-label={t("views.delete", { name: v.name })}
                  onClick={() => void remove(v)}
                >
                  ×
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
      <h3>{t("views.saveTitle")}</h3>
      <form
        className="custom-filter"
        onSubmit={(e) => {
          e.preventDefault();
          if (name.trim()) void save();
        }}
      >
        <label>
          {t("views.name")}
          <input
            value={name}
            maxLength={100}
            placeholder={t("views.namePlaceholder")}
            onChange={(e) => setName(e.target.value)}
          />
        </label>
        <label className="check">
          <input
            type="checkbox"
            checked={shared}
            onChange={(e) => setShared(e.target.checked)}
          />
          {t("views.share")}
        </label>
        <button type="submit" disabled={!name.trim()}>
          {t("views.save")}
        </button>
      </form>
      {error && (
        <p role="alert" className="alert error">
          {t("error", { message: error })}
        </p>
      )}
    </div>
  );
}

export function AccountsPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { canRunBackup, canManagePolicies, canManageSettings } = useAuth();
  const [params, setParams] = useSearchParams();
  const filters = params.getAll("f");
  const q = params.get("q") ?? "";
  const sort = params.get("sort") ?? "email";
  const order = params.get("order") === "desc" ? "desc" : "asc";
  const limit = Number(params.get("limit")) || 50;
  const offset = Number(params.get("offset")) || 0;
  const groupBy = params.get("group") ?? "";
  const [draft, setDraft] = useState(q);
  const [columns, setColumns] = useState<string[]>(loadColumns);
  const [selected, setSelected] = useState<Map<string, AccountItem>>(new Map());
  const [notice, setNotice] = useState<Notice>(null);
  const [busy, setBusy] = useState(false);
  const [quotaOpen, setQuotaOpen] = useState(false);

  const [facets] = useLoad<AccountFacets>(() => api.accountFacets(), []);
  const key = params.toString();
  const [accounts, reload] = useLoad<Page<AccountItem>>(
    () => api.accountList({ q, filters, sort, order, limit, offset }),
    [key],
  );
  const [groups, reloadGroups] = useLoad<AccountGroup[] | null>(
    () =>
      groupBy
        ? api.accountGroups({ groupBy, q: q || undefined, filters })
        : Promise.resolve(null),
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
  const viewQuery = (() => {
    const copy = new URLSearchParams(params);
    copy.delete("offset");
    return copy.toString();
  })();
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
  const togglePage = (items: AccountItem[], select: boolean) =>
    setSelected((prev) => {
      const next = new Map(prev);
      if (select) items.forEach((a) => next.set(a.id, a));
      else items.forEach((a) => next.delete(a.id));
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
  const groupable =
    facets.state === "ready" ? (facets.data.groupable ?? []) : [];
  const groupType: FieldType =
    (facets.state === "ready" && facets.data.fields[groupBy]) || "text";
  const tableProps = {
    visible,
    selected,
    onToggle: toggleOne,
    onTogglePage: togglePage,
    onSort: setSort,
  };

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
        <Dropdown
          label={
            groupBy
              ? t("groups.by", {
                  field: columnLabel(
                    groupBy === "policy" ? "policies" : groupBy,
                  ),
                })
              : t("groups.title")
          }
        >
          {(close) => (
            <ul className="menu">
              {groupable.map((g) => (
                <li key={g}>
                  <label className="check">
                    <input
                      type="radio"
                      name="group-by"
                      checked={groupBy === g}
                      onChange={() => {
                        update({ group: g });
                        close();
                      }}
                    />
                    {columnLabel(g === "policy" ? "policies" : g)}
                  </label>
                </li>
              ))}
              {groupBy && (
                <li>
                  <button
                    type="button"
                    className="link"
                    onClick={() => {
                      update({ group: null });
                      close();
                    }}
                  >
                    {t("groups.clear")}
                  </button>
                </li>
              )}
            </ul>
          )}
        </Dropdown>
        <Dropdown label={t("views.title")}>
          {(close) => (
            <Favorites
              page="accounts"
              current={viewQuery}
              onApply={(query) => {
                setParams(new URLSearchParams(query), { replace: true });
                setDraft(new URLSearchParams(query).get("q") ?? "");
                close();
              }}
            />
          )}
        </Dropdown>
        <Dropdown label={t("columns.picker")}>
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
          {canManageSettings && (
            <button
              type="button"
              className="small secondary"
              aria-expanded={quotaOpen}
              onClick={() => setQuotaOpen(!quotaOpen)}
            >
              {t("quota.title")}
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
      {quotaOpen && canManageSettings && selected.size > 0 && (
        <section className="card">
          <QuotaForm
            accountIds={[...selected.keys()]}
            current={
              selection.length === 1
                ? (selection[0]?.quota_bytes ?? 0)
                : undefined
            }
            onCancel={() => setQuotaOpen(false)}
            onDone={(text) => {
              setNotice({ kind: "ok", text });
              setQuotaOpen(false);
              setSelected(new Map());
              reload();
            }}
          />
        </section>
      )}
      <NoticeBar notice={notice} />

      {groupBy ? (
        <LoadView load={groups} reload={reloadGroups}>
          {(list) =>
            !list || list.length === 0 ? (
              <p className="muted">{t("accounts.empty")}</p>
            ) : (
              <div className="groups">
                <p className="muted small">
                  {t("groups.summary", {
                    groups: list.length,
                    count: list.reduce((n, g) => n + g.count, 0),
                  })}
                </p>
                {list.map((g) => (
                  <GroupSection
                    key={`${groupBy}:${g.value ?? "∅"}`}
                    group={g}
                    field={groupBy}
                    type={groupType}
                    q={q}
                    filters={filters}
                    sort={sort}
                    order={order}
                    tableProps={tableProps}
                    onOpen={(extra) =>
                      update({
                        group: null,
                        f: [
                          ...filters,
                          ...extra.filter((f) => !filters.includes(f)),
                        ],
                      })
                    }
                  />
                ))}
              </div>
            )
          }
        </LoadView>
      ) : (
        <LoadView load={accounts} reload={reload}>
          {(page) =>
            page.items.length === 0 ? (
              <p className="muted">{t("accounts.empty")}</p>
            ) : (
              <>
                <AccountTable
                  {...tableProps}
                  items={page.items}
                  sort={sort}
                  order={order}
                />
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
                        {
                          offset: String(Math.max(0, page.offset - page.limit)),
                        },
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
      )}
    </>
  );
}
