import { useState } from "react";
import type { FormEvent } from "react";
import { useTranslation } from "react-i18next";

import { api } from "../api/endpoints";
import type { Domain, Page, Role, TwoFactorPolicy, User } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { LoadView, errorMessage, useLoad } from "../components/common";
import { NoticeBar, type Notice } from "../components/notices";
import { formatDate, formatRelative } from "../utils/format";

const ROLES: Role[] = [
  "super_admin",
  "domain_admin",
  "restore_operator",
  "viewer",
];

function CreateForm({ onDone }: { onDone: (u: User | null) => void }) {
  const { t } = useTranslation();
  const [source, setSource] = useState<"local" | "ldap">("local");
  const [username, setUsername] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [notice, setNotice] = useState<Notice>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    try {
      onDone(
        await api.createUser({
          username,
          auth_source: source,
          password: source === "local" ? password : null,
          display_name: displayName || null,
          email: email || null,
        }),
      );
    } catch (err) {
      setNotice({
        kind: "error",
        text: t("error", { message: errorMessage(err) }),
      });
    }
  }

  return (
    <form className="card stack" onSubmit={(e) => void submit(e)}>
      <h2>{t("users.new")}</h2>
      <div className="row">
        <label className="inline">
          <input
            type="radio"
            name="source"
            checked={source === "local"}
            onChange={() => setSource("local")}
          />
          {t("users.sources.local")}
        </label>
        <label className="inline">
          <input
            type="radio"
            name="source"
            checked={source === "ldap"}
            onChange={() => setSource("ldap")}
          />
          {t("users.sources.ldap")}
        </label>
      </div>
      <div className="row">
        <label className="grow">
          {t("users.username")}
          <input
            required
            value={username}
            onChange={(e) => setUsername(e.target.value)}
          />
        </label>
        <label className="grow">
          {t("users.displayName")}
          <input
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
          />
        </label>
        <label className="grow">
          {t("users.email")}
          <input
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </label>
        {source === "local" && (
          <label className="grow">
            {t("users.password")}
            <input
              type="password"
              autoComplete="new-password"
              required
              minLength={12}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </label>
        )}
      </div>
      <p className="muted">
        {source === "local" ? t("users.localHelp") : t("users.ldapHelp")}
      </p>
      <NoticeBar notice={notice} />
      <div className="actions">
        <button type="submit">{t("users.create")}</button>
        <button
          type="button"
          className="secondary"
          onClick={() => onDone(null)}
        >
          {t("cancel")}
        </button>
      </div>
    </form>
  );
}

function UserEditor({
  user,
  domains,
  onChanged,
  onClose,
}: {
  user: User;
  domains: Domain[];
  onChanged: () => void;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const { user: me } = useAuth();
  const [displayName, setDisplayName] = useState(user.display_name ?? "");
  const [email, setEmail] = useState(user.email ?? "");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<Role>("viewer");
  const [domain, setDomain] = useState("");
  const [notice, setNotice] = useState<Notice>(null);
  const domainName = (id: string | null) =>
    id ? (domains.find((d) => d.id === id)?.name ?? id) : t("users.allDomains");

  async function act(run: () => Promise<unknown>, ok: string) {
    setNotice(null);
    try {
      await run();
      setNotice({ kind: "ok", text: ok });
      onChanged();
    } catch (err) {
      setNotice({
        kind: "error",
        text: t("error", { message: errorMessage(err) }),
      });
    }
  }

  return (
    <section
      className="card stack"
      aria-label={t("users.editTitle", { user: user.username })}
    >
      <div className="toolbar">
        <h2 className="grow">
          {t("users.editTitle", { user: user.username })}
        </h2>
        <button type="button" className="secondary small" onClick={onClose}>
          {t("users.close")}
        </button>
      </div>
      <NoticeBar notice={notice} />
      <form
        className="row"
        onSubmit={(e) => {
          e.preventDefault();
          void act(
            () =>
              api.updateUser(user.id, {
                display_name: displayName,
                email,
              }),
            t("users.saved"),
          );
        }}
      >
        <label className="grow">
          {t("users.displayName")}
          <input
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
          />
        </label>
        <label className="grow">
          {t("users.email")}
          <input
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </label>
        <button type="submit" className="align-end">
          {t("save")}
        </button>
      </form>

      <h3>{t("users.roles")}</h3>
      {user.roles.length === 0 ? (
        <p className="alert warn">{t("users.noRole")}</p>
      ) : (
        <ul>
          {user.roles.map((r) => (
            <li key={r.id}>
              {t(`users.role.${r.role}`)} — {domainName(r.domain_id)}{" "}
              <button
                type="button"
                className="link"
                onClick={() =>
                  void act(
                    () => api.revokeRole(user.id, r.id),
                    t("users.roleRevoked"),
                  )
                }
              >
                {t("users.revoke")}
              </button>
            </li>
          ))}
        </ul>
      )}
      <div className="row">
        <label>
          {t("users.addRole")}
          <select
            value={role}
            onChange={(e) => setRole(e.target.value as Role)}
          >
            {ROLES.map((r) => (
              <option key={r} value={r}>
                {t(`users.role.${r}`)}
              </option>
            ))}
          </select>
        </label>
        {role !== "super_admin" && (
          <label>
            {t("users.domain")}
            <select value={domain} onChange={(e) => setDomain(e.target.value)}>
              <option value="">{t("users.allDomains")}</option>
              {domains.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.name}
                </option>
              ))}
            </select>
          </label>
        )}
        <button
          type="button"
          className="align-end"
          onClick={() =>
            void act(
              () =>
                api.grantRole(
                  user.id,
                  role,
                  role === "super_admin" ? null : domain || null,
                ),
              t("users.roleGranted"),
            )
          }
        >
          {t("users.grant")}
        </button>
      </div>
      <p className="muted">{t(`users.roleHelp.${role}`)}</p>

      <h3>{t("users.security")}</h3>
      {user.auth_source === "local" && (
        <form
          className="row"
          onSubmit={(e) => {
            e.preventDefault();
            void act(async () => {
              await api.updateUser(user.id, { password });
              setPassword("");
            }, t("users.passwordReset"));
          }}
        >
          <label className="grow">
            {t("users.newPassword")}
            <input
              type="password"
              autoComplete="new-password"
              required
              minLength={12}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </label>
          <button type="submit" className="align-end secondary">
            {t("users.resetPassword")}
          </button>
        </form>
      )}
      <div className="actions wrap-actions">
        {user.totp_enabled && (
          <button
            type="button"
            className="secondary"
            onClick={() => {
              if (
                window.confirm(
                  t("users.confirmReset2fa", { user: user.username }),
                )
              )
                void act(
                  () => api.updateUser(user.id, { reset_2fa: true }),
                  t("users.twoFactorReset"),
                );
            }}
          >
            {t("users.reset2fa")}
          </button>
        )}
        {user.locked_until && new Date(user.locked_until) > new Date() && (
          <button
            type="button"
            className="secondary"
            onClick={() =>
              void act(
                () => api.updateUser(user.id, { unlock: true }),
                t("users.unlocked"),
              )
            }
          >
            {t("users.unlock")}
          </button>
        )}
        {user.id !== me?.id && (
          <>
            <button
              type="button"
              className="secondary"
              onClick={() =>
                void act(
                  () => api.updateUser(user.id, { is_active: !user.is_active }),
                  user.is_active
                    ? t("users.deactivated")
                    : t("users.activated"),
                )
              }
            >
              {user.is_active ? t("users.deactivate") : t("users.activate")}
            </button>
            <button
              type="button"
              className="danger"
              onClick={() => {
                if (
                  window.confirm(
                    t("users.confirmDelete", { user: user.username }),
                  )
                )
                  void act(async () => {
                    await api.deleteUser(user.id);
                    onClose();
                  }, t("users.deleted"));
              }}
            >
              {t("delete")}
            </button>
          </>
        )}
      </div>
    </section>
  );
}

function PolicyCard() {
  const { t } = useTranslation();
  const [load, reload] = useLoad(() => api.securityPolicy(), []);
  const [notice, setNotice] = useState<Notice>(null);
  return (
    <section className="card stack" aria-labelledby="policy-title">
      <h2 id="policy-title">{t("users.policyTitle")}</h2>
      <LoadView load={load} reload={reload}>
        {(p) => (
          <fieldset>
            <legend>{t("users.require2fa")}</legend>
            {(["none", "admins", "all"] as TwoFactorPolicy[]).map((v) => (
              <label key={v} className="inline">
                <input
                  type="radio"
                  name="require-2fa"
                  checked={p.require_2fa === v}
                  onChange={() =>
                    void api
                      .saveSecurityPolicy(v)
                      .then(() => {
                        setNotice({ kind: "ok", text: t("users.policySaved") });
                        reload();
                      })
                      .catch((err: unknown) =>
                        setNotice({
                          kind: "error",
                          text: t("error", { message: errorMessage(err) }),
                        }),
                      )
                  }
                />
                {t(`users.policy.${v}`)}
              </label>
            ))}
          </fieldset>
        )}
      </LoadView>
      <p className="muted">{t("users.policyHelp")}</p>
      <NoticeBar notice={notice} />
    </section>
  );
}

export function UsersPage() {
  const { t } = useTranslation();
  const { canManageUsers } = useAuth();
  const [users, reload] = useLoad<Page<User>>(() => api.users(), []);
  const [domains] = useLoad<Page<Domain>>(() => api.domains(), []);
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);

  if (!canManageUsers) {
    return (
      <p role="alert" className="alert error">
        {t("settings.forbidden")}
      </p>
    );
  }
  const domainList = domains.state === "ready" ? domains.data.items : [];
  const domainName = (id: string | null) =>
    id
      ? (domainList.find((d) => d.id === id)?.name ?? "?")
      : t("users.allDomains");

  return (
    <>
      <div className="toolbar">
        <h1 className="grow">{t("users.title")}</h1>
        <button type="button" onClick={() => setCreating(true)}>
          {t("users.new")}
        </button>
      </div>
      <p className="muted">{t("users.intro")}</p>
      {creating && (
        <CreateForm
          onDone={(u) => {
            setCreating(false);
            if (u) {
              reload();
              setEditing(u.id);
            }
          }}
        />
      )}
      <LoadView load={users} reload={reload}>
        {(page) => {
          const current = page.items.find((u) => u.id === editing);
          return (
            <>
              {current && (
                <UserEditor
                  key={current.id}
                  user={current}
                  domains={domainList}
                  onChanged={reload}
                  onClose={() => {
                    setEditing(null);
                    reload();
                  }}
                />
              )}
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>{t("users.username")}</th>
                      <th>{t("users.displayName")}</th>
                      <th>{t("users.source")}</th>
                      <th>{t("users.roles")}</th>
                      <th>{t("users.twoFactor")}</th>
                      <th>{t("users.state")}</th>
                      <th>{t("users.lastLogin")}</th>
                      <th />
                    </tr>
                  </thead>
                  <tbody>
                    {page.items.map((u) => {
                      const locked =
                        !!u.locked_until &&
                        new Date(u.locked_until) > new Date();
                      return (
                        <tr
                          key={u.id}
                          className={u.id === editing ? "selected" : undefined}
                        >
                          <td>{u.username}</td>
                          <td>{u.display_name ?? "—"}</td>
                          <td>{t(`users.sources.${u.auth_source}`)}</td>
                          <td className="wrap">
                            {u.roles.length ? (
                              u.roles
                                .map(
                                  (r) =>
                                    `${t(`users.role.${r.role}`)} (${domainName(r.domain_id)})`,
                                )
                                .join(", ")
                            ) : (
                              <span className="warn-text">
                                {t("users.noRoleShort")}
                              </span>
                            )}
                          </td>
                          <td>
                            {u.totp_enabled ? (
                              <span className="badge ok">{t("users.on")}</span>
                            ) : (
                              <span className="badge muted">
                                {t("users.off")}
                              </span>
                            )}
                          </td>
                          <td>
                            {locked ? (
                              <span
                                className="badge bad"
                                title={formatDate(u.locked_until)}
                              >
                                {t("users.locked")}
                              </span>
                            ) : (
                              <span
                                className={`badge ${u.is_active ? "ok" : "muted"}`}
                              >
                                {u.is_active
                                  ? t("users.active")
                                  : t("users.inactive")}
                              </span>
                            )}
                          </td>
                          <td>
                            {u.last_login_at
                              ? formatRelative(u.last_login_at)
                              : t("accounts.never")}
                          </td>
                          <td>
                            <button
                              type="button"
                              className="small secondary"
                              onClick={() => setEditing(u.id)}
                            >
                              {t("edit")}
                            </button>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </>
          );
        }}
      </LoadView>
      <PolicyCard />
    </>
  );
}
