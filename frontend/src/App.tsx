import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { useTranslation } from "react-i18next";

import { AuthProvider, useAuth } from "./auth/AuthContext";
import { Layout } from "./components/Layout";
import { AccountPage } from "./pages/AccountPage";
import {
  AccountSecurityPage,
  EnrollmentGate,
} from "./pages/AccountSecurityPage";
import { AccountsPage } from "./pages/AccountsPage";
import { DashboardPage } from "./pages/DashboardPage";
import { JobsPage } from "./pages/JobsPage";
import { LoginPage } from "./pages/LoginPage";
import { MailTrafficPage } from "./pages/MailTrafficPage";
import { MonitoringPage } from "./pages/MonitoringPage";
import { ArchivesPage } from "./pages/ArchivesPage";
import { LogsPage } from "./pages/LogsPage";
import { MigrationBatchPage } from "./pages/MigrationBatchPage";
import { MigrationsPage } from "./pages/MigrationsPage";
import { PoliciesPage } from "./pages/PoliciesPage";
import { RestorePage } from "./pages/RestorePage";
import { SettingsPage } from "./pages/SettingsPage";
import { BlockingPage } from "./pages/BlockingPage";
import { StandbyPage } from "./pages/StandbyPage";
import { UsersPage } from "./pages/UsersPage";

export function AppRoutes() {
  const { t } = useTranslation();
  const { user, loading } = useAuth();
  if (loading) return <p className="muted center">{t("loading")}</p>;
  if (!user) return <LoginPage />;
  if (user.totp_enrollment_required) return <EnrollmentGate />;
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<DashboardPage />} />
        <Route path="accounts" element={<AccountsPage />} />
        <Route path="accounts/:id" element={<AccountPage />} />
        <Route
          path="accounts/:id/snapshots/:snapshotId/restore"
          element={<RestorePage />}
        />
        <Route path="jobs" element={<JobsPage />} />
        <Route path="policies" element={<PoliciesPage />} />
        <Route path="settings" element={<SettingsPage />} />
        <Route path="migrations" element={<MigrationsPage />} />
        <Route path="mail" element={<MailTrafficPage />} />
        <Route path="monitoring" element={<MonitoringPage />} />
        <Route path="archives" element={<ArchivesPage />} />
        <Route path="logs" element={<LogsPage />} />
        <Route path="users" element={<UsersPage />} />
        <Route path="standby" element={<StandbyPage />} />
        <Route path="blocking" element={<BlockingPage />} />
        <Route path="me" element={<AccountSecurityPage />} />
        <Route path="migrations/:id" element={<MigrationBatchPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}

export function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <AppRoutes />
      </AuthProvider>
    </BrowserRouter>
  );
}
