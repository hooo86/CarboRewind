import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import type { ReactNode } from "react";

import { onUnauthorized, setCsrfToken } from "../api/client";
import { api } from "../api/endpoints";
import type { User } from "../api/types";

interface AuthState {
  user: User | null;
  loading: boolean;
  login: (username: string, password: string, otp?: string) => Promise<void>;
  logout: () => Promise<void>;
  /** Reload the current user (after enrolling 2FA, changing the profile…). */
  refresh: () => Promise<void>;
  /** Access accounts management: global super administrators only. */
  canManageUsers: boolean;
  /** True if the user may start backups on this domain (the API enforces it anyway). */
  canRunBackup: (domainId: string) => boolean;
  /** Restore and see item subjects (restore:run, content:preview). */
  canRestore: (domainId: string) => boolean;
  /** Manage policies on a domain, or global ones when ``domainId`` is null. */
  canManagePolicies: (domainId: string | null) => boolean;
  /** Global settings (notifications): super-admins only. */
  canManageSettings: boolean;
  /** Mailbox migrations: global super administrators only (migrations:manage). */
  canManageMigrations: boolean;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    api
      .me()
      .then((me) => !cancelled && setUser(me))
      .catch(() => !cancelled && setUser(null))
      .finally(() => !cancelled && setLoading(false));
    const unsubscribe = onUnauthorized(() => setUser(null));
    return () => {
      cancelled = true;
      unsubscribe();
    };
  }, []);

  const login = useCallback(
    async (username: string, password: string, otp?: string) => {
      const session = await api.login(username, password, otp);
      setCsrfToken(session.csrf_token);
      setUser({
        ...session.user,
        totp_enrollment_required: session.totp_enrollment_required ?? false,
      });
    },
    [],
  );

  const refresh = useCallback(async () => {
    setUser(await api.me());
  }, []);

  const logout = useCallback(async () => {
    try {
      await api.logout();
    } finally {
      setCsrfToken(undefined);
      setUser(null);
    }
  }, []);

  const canRunBackup = useCallback(
    (domainId: string) =>
      (user?.roles ?? []).some(
        (r) =>
          r.role === "super_admin" ||
          (r.role === "domain_admin" &&
            (r.domain_id === null || r.domain_id === domainId)),
      ),
    [user],
  );

  const canRestore = useCallback(
    (domainId: string) =>
      (user?.roles ?? []).some(
        (r) =>
          r.role === "super_admin" ||
          ((r.role === "domain_admin" || r.role === "restore_operator") &&
            (r.domain_id === null || r.domain_id === domainId)),
      ),
    [user],
  );

  const canManagePolicies = useCallback(
    (domainId: string | null) =>
      (user?.roles ?? []).some(
        (r) =>
          r.role === "super_admin" ||
          (r.role === "domain_admin" &&
            (r.domain_id === null ||
              (domainId !== null && r.domain_id === domainId))),
      ),
    [user],
  );

  const canManageSettings = (user?.roles ?? []).some(
    (r) => r.role === "super_admin",
  );
  const canManageMigrations = canManageSettings;
  const canManageUsers = canManageSettings;

  const value = useMemo(
    () => ({
      canManageSettings,
      canManageMigrations,
      canManageUsers,
      refresh,
      user,
      loading,
      login,
      logout,
      canRunBackup,
      canRestore,
      canManagePolicies,
    }),
    [
      user,
      loading,
      login,
      logout,
      canRunBackup,
      canRestore,
      canManagePolicies,
      canManageSettings,
      canManageMigrations,
      canManageUsers,
      refresh,
    ],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth outside AuthProvider");
  return ctx;
}
