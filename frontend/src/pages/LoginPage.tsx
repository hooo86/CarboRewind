import { useState } from "react";
import type { FormEvent } from "react";
import { useTranslation } from "react-i18next";

import { ApiError } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { errorMessage } from "../components/common";
import { LanguageSelect } from "../components/Layout";
import { Logo } from "../components/Logo";

export function LoginPage() {
  const { t } = useTranslation();
  const { login } = useAuth();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  // Second step: the password was right, a code from the authenticator app is needed.
  const [otpStep, setOtpStep] = useState(false);
  const [otp, setOtp] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(username, password, otpStep ? otp : undefined);
    } catch (err) {
      if (err instanceof ApiError && err.detail === "otp_required") {
        setOtpStep(true);
        return;
      }
      if (otpStep && err instanceof ApiError && err.status === 401) {
        setOtp("");
        setError(t("login.badCode"));
        return;
      }
      setPassword("");
      setOtpStep(false);
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
          <h1>
            <Logo size={40} withName />
          </h1>
          <LanguageSelect />
        </div>
        <p className="muted">{t("tagline")}</p>
        <h2>{otpStep ? t("login.otpTitle") : t("login.title")}</h2>
        {otpStep ? (
          <>
            <p className="muted">{t("login.otpHelp")}</p>
            <label>
              {t("login.otp")}
              <input
                name="otp"
                autoComplete="one-time-code"
                inputMode="text"
                autoFocus
                required
                maxLength={32}
                value={otp}
                onChange={(e) => setOtp(e.target.value)}
              />
            </label>
            <button
              type="button"
              className="link"
              onClick={() => {
                setOtpStep(false);
                setOtp("");
                setPassword("");
              }}
            >
              {t("login.back")}
            </button>
          </>
        ) : (
          <>
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
          </>
        )}
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
