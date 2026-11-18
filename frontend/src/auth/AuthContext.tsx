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
  login: (username: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  /** True if the user may start backups on this domain (the API enforces it anyway). */
  canRunBackup: (domainId: string) => boolean;
  /** Restore and see item subjects (restore:run, content:preview). */
  canRestore: (domainId: string) => boolean;
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

  const login = useCallback(async (username: string, password: string) => {
    const session = await api.login(username, password);
    setCsrfToken(session.csrf_token);
    setUser(session.user);
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

  const value = useMemo(
    () => ({ user, loading, login, logout, canRunBackup, canRestore }),
    [user, loading, login, logout, canRunBackup, canRestore],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth outside AuthProvider");
  return ctx;
}
