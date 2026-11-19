import { Link, NavLink, Outlet } from "react-router-dom";
import { useTranslation } from "react-i18next";

import { useAuth } from "../auth/AuthContext";
import { setLanguage, type Language } from "../i18n";
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

export function Layout() {
  const { t } = useTranslation();
  const {
    user,
    logout,
    canManageSettings,
    canManageMigrations,
    canManageUsers,
  } = useAuth();
  // Archived messages are mail content: not for read-only viewers.
  const canReadArchives = (user?.roles ?? []).some((r) => r.role !== "viewer");
  return (
    <div className="shell">
      <aside className="sidebar">
        <span className="brand">
          <Logo size={30} withName />
        </span>
        <nav aria-label="main">
          <NavLink to="/" end>
            {t("nav.dashboard")}
          </NavLink>
          <NavLink to="/accounts">{t("nav.accounts")}</NavLink>
          <NavLink to="/jobs">{t("nav.jobs")}</NavLink>
          <NavLink to="/policies">{t("nav.policies")}</NavLink>
          {canManageSettings && (
            <NavLink to="/monitoring">{t("nav.monitoring")}</NavLink>
          )}
          {canManageSettings && <NavLink to="/mail">{t("nav.mail")}</NavLink>}
          {canReadArchives && (
            <NavLink to="/archives">{t("nav.archives")}</NavLink>
          )}
          {canManageMigrations && (
            <NavLink to="/migrations">{t("nav.migrations")}</NavLink>
          )}
          {canManageUsers && <NavLink to="/users">{t("nav.users")}</NavLink>}
          {canManageSettings && (
            <NavLink to="/settings">{t("nav.settings")}</NavLink>
          )}
        </nav>
        <div className="user">
          <Link to="/me" className="who" title={t("nav.me")}>
            {user?.display_name ?? user?.username}
          </Link>
          <LanguageSelect />
          <button
            type="button"
            className="secondary"
            onClick={() => void logout()}
          >
            {t("logout")}
          </button>
        </div>
      </aside>
      <main className="content">
        <Outlet />
      </main>
    </div>
  );
}
