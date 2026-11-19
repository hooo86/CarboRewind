import { useState } from "react";
import { Link, NavLink, Outlet, useLocation } from "react-router-dom";
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

export function ThemeSelect() {
  const { t } = useTranslation();
  const [theme, set] = useState<Theme>(savedTheme);
  return (
    <label className="lang">
      <span className="sr-only">{t("theme.label")}</span>
      <select
        aria-label={t("theme.label")}
        value={theme}
        onChange={(e) => {
          const value = e.target.value as Theme;
          set(value);
          setTheme(value);
        }}
      >
        <option value="system">{t("theme.system")}</option>
        <option value="light">{t("theme.light")}</option>
        <option value="dark">{t("theme.dark")}</option>
      </select>
    </label>
  );
}

interface Entry {
  to: string;
  icon: IconName;
  label: string;
  end?: boolean;
}

/** A group of the top menu: opens on hover or keyboard focus (CSS :focus-within). */
function MenuGroup({
  label,
  icon,
  entries,
}: {
  label: string;
  icon: IconName;
  entries: Entry[];
}) {
  const { pathname } = useLocation();
  if (entries.length === 0) return null;
  const active = entries.some((e) =>
    e.end ? pathname === e.to : pathname.startsWith(e.to),
  );
  return (
    <div className={`menu-group${active ? " active" : ""}`}>
      <button type="button" className="menu-button" aria-haspopup="true">
        <Icon name={icon} />
        <span>{label}</span>
        <span className="caret" aria-hidden="true">
          ▾
        </span>
      </button>
      <div className="menu-panel">
        {entries.map((e) => (
          <NavLink key={e.to} to={e.to} end={e.end}>
            <Icon name={e.icon} />
            <span>{e.label}</span>
          </NavLink>
        ))}
      </div>
    </div>
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
  const when = (ok: boolean, e: Entry): Entry[] => (ok ? [e] : []);
  return (
    <div className="shell">
      <header className="topbar">
        <Link to="/" className="brand" aria-label="CarboRewind">
          <Logo size={28} withName />
          <span className="edition">Carbonio CE</span>
        </Link>
        <nav aria-label="main" className="topnav">
          <MenuGroup
            label={t("nav.groupBackup")}
            icon="dashboard"
            entries={[
              {
                to: "/",
                icon: "dashboard",
                label: t("nav.dashboard"),
                end: true,
              },
              { to: "/accounts", icon: "mail", label: t("nav.accounts") },
              { to: "/jobs", icon: "jobs", label: t("nav.jobs") },
              { to: "/policies", icon: "policy", label: t("nav.policies") },
            ]}
          />
          <MenuGroup
            label={t("nav.groupMail")}
            icon="traffic"
            entries={[
              ...when(canManageSettings, {
                to: "/monitoring",
                icon: "monitoring",
                label: t("nav.monitoring"),
              }),
              ...when(canManageSettings, {
                to: "/mail",
                icon: "traffic",
                label: t("nav.mail"),
              }),
              ...when(canReadArchives, {
                to: "/archives",
                icon: "archive",
                label: t("nav.archives"),
              }),
              ...when(canManageMigrations, {
                to: "/migrations",
                icon: "migration",
                label: t("nav.migrations"),
              }),
            ]}
          />
          <MenuGroup
            label={t("nav.configuration")}
            icon="settings"
            entries={[
              ...when(canManageUsers, {
                to: "/users",
                icon: "users",
                label: t("nav.users"),
              }),
              ...when(canManageSettings, {
                to: "/settings",
                icon: "settings",
                label: t("nav.settings"),
              }),
            ]}
          />
          <div className="menu-group user-menu">
            <button type="button" className="menu-button" aria-haspopup="true">
              <span className="avatar" aria-hidden="true">
                {initials}
              </span>
              <span className="who-name">
                {user?.display_name ?? user?.username}
              </span>
              <span className="caret" aria-hidden="true">
                ▾
              </span>
            </button>
            <div className="menu-panel">
              <Link to="/me" className="who" title={t("nav.me")}>
                <Icon name="user" />
                <span>{t("nav.me")}</span>
              </Link>
              <div className="menu-row">
                <ThemeSelect />
                <LanguageSelect />
              </div>
              <button
                type="button"
                className="secondary small"
                onClick={() => void logout()}
              >
                <Icon name="logout" /> {t("logout")}
              </button>
            </div>
          </div>
        </nav>
      </header>
      <main className="content">
        <Outlet />
      </main>
    </div>
  );
}
