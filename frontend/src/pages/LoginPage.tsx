import { useState } from "react";
import type { FormEvent } from "react";
import { useTranslation } from "react-i18next";

import { ApiError } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { errorMessage } from "../components/common";
import { LanguageSelect } from "../components/Layout";

export function LoginPage() {
  const { t } = useTranslation();
  const { login } = useAuth();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(username, password);
    } catch (err) {
      setPassword("");
      if (err instanceof ApiError && err.status === 401)
        setError(t("login.invalid"));
      else if (err instanceof ApiError && err.status === 429)
        setError(t("login.throttled"));
      else setError(t("login.failed", { message: errorMessage(err) }));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login">
      <form className="card login-card" onSubmit={(e) => void submit(e)}>
        <div className="login-head">
          <h1>CarboRewind</h1>
          <LanguageSelect />
        </div>
        <p className="muted">{t("tagline")}</p>
        <h2>{t("login.title")}</h2>
        <label>
          {t("login.username")}
          <input
            name="username"
            autoComplete="username"
            required
            value={username}
            onChange={(e) => setUsername(e.target.value)}
          />
        </label>
        <label>
          {t("login.password")}
          <input
            name="password"
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </label>
        {error && (
          <p role="alert" className="alert error">
            {error}
          </p>
        )}
        <button type="submit" disabled={busy}>
          {busy ? t("login.submitting") : t("login.submit")}
        </button>
      </form>
    </div>
  );
}
