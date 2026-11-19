import { useState } from "react";
import type { FormEvent } from "react";
import { useTranslation } from "react-i18next";

import { api } from "../api/endpoints";
import type { TotpSetup } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { errorMessage } from "../components/common";
import { Logo } from "../components/Logo";
import { NoticeBar, type Notice } from "../components/notices";

function svgDataUri(svg: string): string {
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
}

export function RecoveryCodes({ codes }: { codes: string[] }) {
  const { t } = useTranslation();
  return (
    <div className="card stack recovery">
      <h3>{t("security.recoveryTitle")}</h3>
      <p className="alert warn">{t("security.recoveryHelp")}</p>
      <ul className="codes" aria-label={t("security.recoveryTitle")}>
        {codes.map((c) => (
          <li key={c}>
            <code>{c}</code>
          </li>
        ))}
      </ul>
      <div className="actions">
        <button
          type="button"
          className="secondary small"
          onClick={() => void navigator.clipboard?.writeText(codes.join("\n"))}
        >
          {t("security.copyCodes")}
        </button>
      </div>
    </div>
  );
}

/** Enrolment: QR code (or secret), first code, then the recovery codes. */
export function TwoFactorSetup({ onDone }: { onDone: () => void }) {
  const { t } = useTranslation();
  const [setup, setSetup] = useState<TotpSetup | null>(null);
  const [code, setCode] = useState("");
  const [codes, setCodes] = useState<string[] | null>(null);
  const [notice, setNotice] = useState<Notice>(null);
  const [busy, setBusy] = useState(false);

  async function start() {
    setBusy(true);
    setNotice(null);
    try {
      setSetup(await api.totpSetup());
    } catch (err) {
      setNotice({
        kind: "error",
        text: t("error", { message: errorMessage(err) }),
      });
    } finally {
      setBusy(false);
    }
  }

  async function confirm(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setNotice(null);
    try {
      setCodes((await api.totpEnable(code.trim())).recovery_codes);
    } catch (err) {
      setCode("");
      setNotice({
        kind: "error",
        text: t("security.badCode", { message: errorMessage(err) }),
      });
    } finally {
      setBusy(false);
    }
  }

  if (codes) {
    return (
      <>
        <p className="alert ok" role="status">
          {t("security.enabled")}
        </p>
        <RecoveryCodes codes={codes} />
        <button type="button" onClick={onDone}>
          {t("security.codesSaved")}
        </button>
      </>
    );
  }
  if (!setup) {
    return (
      <div className="stack">
        <p>{t("security.intro")}</p>
        <NoticeBar notice={notice} />
        <div className="actions">
          <button type="button" disabled={busy} onClick={() => void start()}>
            {t("security.start")}
          </button>
        </div>
      </div>
    );
  }
  return (
    <form className="stack" onSubmit={(e) => void confirm(e)}>
      <ol className="steps">
        <li>{t("security.step1")}</li>
        <li>
          {t("security.step2")}
          <div className="qr">
            <img
              src={svgDataUri(setup.qr_svg)}
              alt={t("security.qrAlt")}
              width={200}
              height={200}
            />
          </div>
          <details>
            <summary>{t("security.manual")}</summary>
            <p>
              <code className="secret">
                {setup.secret.match(/.{1,4}/g)?.join(" ")}
              </code>
            </p>
          </details>
        </li>
        <li>
          <label>
            {t("security.step3")}
            <input
              autoComplete="one-time-code"
              inputMode="numeric"
              pattern="[0-9 ]{6,8}"
              required
              maxLength={8}
              value={code}
              onChange={(e) => setCode(e.target.value)}
            />
          </label>
        </li>
      </ol>
      <NoticeBar notice={notice} />
      <div className="actions">
        <button type="submit" disabled={busy}>
          {t("security.confirm")}
        </button>
      </div>
    </form>
  );
}

/** Full-screen enrolment when the security policy requires 2FA and the user has none. */
export function EnrollmentGate() {
  const { t } = useTranslation();
  const { user, refresh, logout } = useAuth();
  return (
    <div className="login">
      <div className="card login-card enrol">
        <div className="login-head">
          <h1>
            <Logo size={36} withName />
          </h1>
        </div>
        <h2>{t("security.requiredTitle")}</h2>
        <p className="muted">
          {t("security.requiredHelp", { user: user?.username ?? "" })}
        </p>
        <TwoFactorSetup onDone={() => void refresh()} />
        <button type="button" className="link" onClick={() => void logout()}>
          {t("logout")}
        </button>
      </div>
    </div>
  );
}

function PasswordForm() {
  const { t } = useTranslation();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [again, setAgain] = useState("");
  const [notice, setNotice] = useState<Notice>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (next !== again) {
      setNotice({ kind: "error", text: t("security.mismatch") });
      return;
    }
    try {
      await api.changePassword(current, next);
      setCurrent("");
      setNext("");
      setAgain("");
      setNotice({ kind: "ok", text: t("security.passwordChanged") });
    } catch (err) {
      setNotice({
        kind: "error",
        text: t("error", { message: errorMessage(err) }),
      });
    }
  }

  return (
    <form className="card stack" onSubmit={(e) => void submit(e)}>
      <h2>{t("security.password")}</h2>
      <div className="row">
        <label className="grow">
          {t("security.current")}
          <input
            type="password"
            autoComplete="current-password"
            required
            value={current}
            onChange={(e) => setCurrent(e.target.value)}
          />
        </label>
        <label className="grow">
          {t("security.new")}
          <input
            type="password"
            autoComplete="new-password"
            required
            minLength={12}
            value={next}
            onChange={(e) => setNext(e.target.value)}
          />
        </label>
        <label className="grow">
          {t("security.again")}
          <input
            type="password"
            autoComplete="new-password"
            required
            value={again}
            onChange={(e) => setAgain(e.target.value)}
          />
        </label>
      </div>
      <p className="muted">{t("security.passwordHelp")}</p>
      <NoticeBar notice={notice} />
      <div className="actions">
        <button type="submit">{t("security.change")}</button>
      </div>
    </form>
  );
}

function TwoFactorCard() {
  const { t } = useTranslation();
  const { user, refresh } = useAuth();
  const [enrolling, setEnrolling] = useState(false);
  const [code, setCode] = useState("");
  const [codes, setCodes] = useState<string[] | null>(null);
  const [notice, setNotice] = useState<Notice>(null);

  async function run(action: "disable" | "codes") {
    setNotice(null);
    try {
      if (action === "disable") {
        await api.totpDisable(code.trim());
        setNotice({ kind: "ok", text: t("security.disabled") });
        await refresh();
      } else {
        setCodes((await api.totpRecoveryCodes(code.trim())).recovery_codes);
      }
      setCode("");
    } catch (err) {
      setNotice({
        kind: "error",
        text: t("security.badCode", { message: errorMessage(err) }),
      });
    }
  }

  return (
    <section className="card stack" aria-labelledby="tfa-title">
      <h2 id="tfa-title">{t("security.twoFactor")}</h2>
      {user?.totp_enabled ? (
        <>
          <p>
            <span className="badge ok">{t("security.on")}</span>{" "}
            {t("security.codesLeft", { n: user.recovery_codes_left ?? 0 })}
          </p>
          <label>
            {t("security.codeForAction")}
            <input
              autoComplete="one-time-code"
              maxLength={32}
              value={code}
              onChange={(e) => setCode(e.target.value)}
            />
          </label>
          <div className="actions">
            <button
              type="button"
              className="secondary"
              disabled={!code}
              onClick={() => void run("codes")}
            >
              {t("security.newCodes")}
            </button>
            <button
              type="button"
              className="secondary"
              disabled={!code}
              onClick={() => void run("disable")}
            >
              {t("security.disable")}
            </button>
          </div>
          {codes && <RecoveryCodes codes={codes} />}
        </>
      ) : enrolling ? (
        <TwoFactorSetup
          onDone={() => {
            setEnrolling(false);
            void refresh();
          }}
        />
      ) : (
        <>
          <p>
            <span className="badge warn">{t("security.off")}</span>{" "}
            {t("security.offHelp")}
          </p>
          <div className="actions">
            <button type="button" onClick={() => setEnrolling(true)}>
              {t("security.enable")}
            </button>
          </div>
        </>
      )}
      <NoticeBar notice={notice} />
    </section>
  );
}

export function AccountSecurityPage() {
  const { t } = useTranslation();
  const { user } = useAuth();
  return (
    <>
      <h1>{t("security.title")}</h1>
      <p className="muted">
        {t("security.signedInAs", {
          user: user?.display_name ?? user?.username ?? "",
          source: t(`users.sources.${user?.auth_source ?? "local"}`),
        })}
      </p>
      {user?.auth_source === "local" ? (
        <PasswordForm />
      ) : (
        <p className="alert info">{t("security.directoryPassword")}</p>
      )}
      <TwoFactorCard />
    </>
  );
}
