import { useEffect, useRef, useState } from "react";
import { Link, NavLink, Outlet } from "react-router-dom";
import { useTranslation } from "react-i18next";

import { useAuth } from "../auth/AuthContext";
import { setLanguage, type Language } from "../i18n";
import { savedTheme, setTheme, type Theme } from "../theme";
import { Icon, type IconName } from "./Icon";
import { Logo } from "./Logo";

export function LanguageSelect() {
  const { t, i18n } = useTranslation();
  return (
    <label className="lang">
      <span className="sr-only">{t("language")}</span>
      <select
        aria-label={t("language")}
        value={i18n.language}
        onChange={(e) => setLanguage(e.target.value as Language)}
      >
        <option value="fr">FR</option>
        <option value="en">EN</option>
      </select>
    </label>
  );
}

const ROLE_ORDER = [
  "super_admin",
  "domain_admin",
  "restore_operator",
  "viewer",
] as const;

/** Account menu at the top right: identity, language, theme, account, settings, sign out. */
function UserMenu() {
  const { t, i18n } = useTranslation();
  const { user, logout, canManageSettings } = useAuth();
  const [open, setOpen] = useState(false);
  const [theme, setThemeState] = useState<Theme>(savedTheme);
  const box = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const close = (event: MouseEvent | KeyboardEvent) => {
      if (event instanceof KeyboardEvent) {
        if (event.key === "Escape") setOpen(false);
      } else if (box.current && !box.current.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", close);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", close);
    };
  }, [open]);

  const name = user?.display_name ?? user?.username ?? "";
  const initials = name
    .split(/\s+/)
    .map((w) => w[0])
    .join("")
    .slice(0, 2)
    .toUpperCase();
  const roles = new Set((user?.roles ?? []).map((r) => r.role));
  const role = ROLE_ORDER.find((r) => roles.has(r));
  const pickTheme = (value: Theme) => {
    setThemeState(value);
    setTheme(value);
  };

  return (
    <div className="user-menu" ref={box}>
      <button
        type="button"
        className="user-button"
        aria-haspopup="true"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        <span className="avatar" aria-hidden="true">
          {initials || "?"}
        </span>
        <span className="who-name">{name}</span>
        <span className="chevron" aria-hidden="true">
          <svg
            width="14"
            height="14"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
          >
            <path d="M6 9l6 6 6-6" />
          </svg>
        </span>
      </button>
      {open && (
        <div
          className="user-panel"
          role="dialog"
          aria-label={t("nav.userMenu")}
        >
          <div className="user-head">
            <span className="avatar large" aria-hidden="true">
              {initials || "?"}
            </span>
            <div>
              <strong>{name}</strong>
              {role && <p className="muted small">{t(`users.role.${role}`)}</p>}
            </div>
          </div>
          <div className="user-section">
            <span className="user-label">{t("language")}</span>
            <div className="segmented" role="group" aria-label={t("language")}>
              {(["fr", "en"] as Language[]).map((lang) => (
                <button
                  key={lang}
                  type="button"
                  aria-pressed={i18n.language === lang}
                  onClick={() => setLanguage(lang)}
                >
                  {lang === "fr" ? "Français" : "English"}
                </button>
              ))}
            </div>
          </div>
          <div className="user-section">
            <span className="user-label">{t("theme.label")}</span>
            <div
              className="segmented"
              role="group"
              aria-label={t("theme.label")}
            >
              {(["light", "dark", "system"] as Theme[]).map((value) => (
                <button
                  key={value}
                  type="button"
                  aria-pressed={theme === value}
                  onClick={() => pickTheme(value)}
                >
                  {t(`theme.${value}`)}
                </button>
              ))}
            </div>
          </div>
          <div className="user-links">
            <Link to="/me" onClick={() => setOpen(false)}>
              <Icon name="policy" />
              <span className="grow">{t("nav.security")}</span>
              <span className={`badge ${user?.totp_enabled ? "ok" : "warn"}`}>
                {t(user?.totp_enabled ? "nav.twoFactorOn" : "nav.twoFactorOff")}
              </span>
            </Link>
            {canManageSettings && (
              <Link to="/settings" onClick={() => setOpen(false)}>
                <Icon name="settings" />
                <span className="grow">{t("nav.settings")}</span>
              </Link>
            )}
            <button
              type="button"
              className="logout-item"
              onClick={() => void logout()}
            >
              <Icon name="logout" />
              <span>{t("logout")}</span>
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

export function Layout() {
  const { t } = useTranslation();
  const { user, canManageSettings, canManageMigrations, canManageUsers } =
    useAuth();
  // Archived messages are mail content: not for read-only viewers.
  const canReadArchives = (user?.roles ?? []).some((r) => r.role !== "viewer");
  const item = (to: string, icon: IconName, label: string, end = false) => (
    <NavLink to={to} end={end}>
      <Icon name={icon} />
      <span>{label}</span>
    </NavLink>
  );
  return (
    <div className="shell">
      <aside className="sidebar">
        <span className="brand">
          <Logo size={30} withName />
          <span className="edition">Carbonio CE</span>
        </span>
        <nav aria-label="main">
          <p className="nav-section" aria-hidden="true">
            {t("nav.administration")}
          </p>
          {item("/", "dashboard", t("nav.dashboard"), true)}
          {item("/accounts", "mail", t("nav.accounts"))}
          {item("/jobs", "jobs", t("nav.jobs"))}
          {item("/policies", "policy", t("nav.policies"))}
          {canManageSettings &&
            item("/monitoring", "monitoring", t("nav.monitoring"))}
          {canManageSettings && item("/mail", "traffic", t("nav.mail"))}
          {canReadArchives && item("/archives", "archive", t("nav.archives"))}
          {canManageMigrations &&
            item("/migrations", "migration", t("nav.migrations"))}
          {(canManageUsers || canManageSettings) && (
            <p className="nav-section" aria-hidden="true">
              {t("nav.configuration")}
            </p>
          )}
          {canManageUsers && item("/users", "users", t("nav.users"))}
          {canManageSettings &&
            item("/settings", "settings", t("nav.settings"))}
        </nav>
      </aside>
      <div className="main-col">
        <header className="appbar">
          <UserMenu />
        </header>
        <main className="content">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
