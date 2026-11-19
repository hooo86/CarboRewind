import { Link, NavLink, Outlet } from "react-router-dom";
import { useTranslation } from "react-i18next";

import { useAuth } from "../auth/AuthContext";
import { setLanguage, type Language } from "../i18n";
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
  const initials = (user?.display_name ?? user?.username ?? "?")
    .split(/\s+/)
    .map((w) => w[0])
    .join("")
    .slice(0, 2)
    .toUpperCase();
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
        <div className="user">
          <span className="avatar" aria-hidden="true">
            {initials}
          </span>
          <Link to="/me" className="who" title={t("nav.me")}>
            {user?.display_name ?? user?.username}
          </Link>
          <LanguageSelect />
          <button
            type="button"
            className="secondary icon-button"
            aria-label={t("logout")}
            title={t("logout")}
            onClick={() => void logout()}
          >
            <Icon name="logout" />
          </button>
        </div>
      </aside>
      <main className="content">
        <Outlet />
      </main>
    </div>
  );
}
