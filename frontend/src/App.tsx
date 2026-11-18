import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { useTranslation } from "react-i18next";

import { AuthProvider, useAuth } from "./auth/AuthContext";
import { Layout } from "./components/Layout";
import { AccountPage } from "./pages/AccountPage";
import { AccountsPage } from "./pages/AccountsPage";
import { DashboardPage } from "./pages/DashboardPage";
import { JobsPage } from "./pages/JobsPage";
import { LoginPage } from "./pages/LoginPage";
import { MigrationBatchPage } from "./pages/MigrationBatchPage";
import { MigrationsPage } from "./pages/MigrationsPage";
import { PoliciesPage } from "./pages/PoliciesPage";
import { RestorePage } from "./pages/RestorePage";
import { SettingsPage } from "./pages/SettingsPage";

export function AppRoutes() {
  const { t } = useTranslation();
  const { user, loading } = useAuth();
  if (loading) return <p className="muted center">{t("loading")}</p>;
  if (!user) return <LoginPage />;
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
