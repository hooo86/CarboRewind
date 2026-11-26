import { useState } from "react";
import type { FormEvent } from "react";
import { useTranslation } from "react-i18next";

import { api } from "../api/endpoints";
import { errorMessage } from "./common";
import { NoticeBar, type Notice } from "./notices";

const ALPHABETS = [
  "ABCDEFGHJKLMNPQRSTUVWXYZ",
  "abcdefghijkmnopqrstuvwxyz",
  "23456789",
  "-_.!@#%+=",
];

function randomIndex(n: number): number {
  const buf = new Uint32Array(1);
  crypto.getRandomValues(buf);
  return (buf[0] ?? 0) % n;
}

/** Random password (browser's secure generator): 16 characters, every class present,
 * without look-alike characters (0/O, 1/l/I). */
export function generatePassword(length = 16): string {
  const all = ALPHABETS.join("");
  const chars = ALPHABETS.map((a) => a[randomIndex(a.length)] ?? "");
  while (chars.length < length) chars.push(all[randomIndex(all.length)] ?? "");
  for (let i = chars.length - 1; i > 0; i--) {
    const j = randomIndex(i + 1);
    [chars[i], chars[j]] = [chars[j] ?? "", chars[i] ?? ""];
  }
  return chars.join("");
}

/** New password for one mailbox, applied by the agent within a minute. */
export function PasswordForm({
  accountId,
  email,
  onCancel,
  onDone,
}: {
  accountId: string;
  email: string;
  onCancel: () => void;
  onDone: (text: string) => void;
}) {
  const { t } = useTranslation();
  const [password, setPassword] = useState("");
  const [again, setAgain] = useState("");
  const [shown, setShown] = useState(false);
  const [mustChange, setMustChange] = useState(true);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<Notice>(null);
  const valid = password.length >= 8 && password === again;

  const generate = () => {
    const value = generatePassword();
    setPassword(value);
    setAgain(value);
    setShown(true);
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setNotice(null);
    try {
      const r = await api.resetPassword(accountId, password, mustChange);
      setPassword("");
      setAgain("");
      onDone(
        r.queued
          ? t("password.queued", { email })
          : t("mailbox.alreadyPending"),
      );
    } catch (err) {
      setNotice({
        kind: "error",
        text: t("error", { message: errorMessage(err) }),
      });
    } finally {
      setBusy(false);
    }
  };

  return (
    <form
      className="stack"
      onSubmit={(e) => void submit(e)}
      aria-label={t("password.title")}
    >
      <p className="muted small">{t("password.help")}</p>
      <div className="row">
        <label className="grow">
          {t("password.new")}
          <input
            type={shown ? "text" : "password"}
            value={password}
            minLength={8}
            maxLength={128}
            autoComplete="new-password"
            onChange={(e) => setPassword(e.target.value)}
          />
        </label>
        <label className="grow">
          {t("password.again")}
          <input
            type={shown ? "text" : "password"}
            value={again}
            maxLength={128}
            autoComplete="new-password"
            onChange={(e) => setAgain(e.target.value)}
          />
        </label>
      </div>
      <div className="actions">
        <button type="button" className="secondary small" onClick={generate}>
          {t("password.generate")}
        </button>
        <label className="inline">
          <input
            type="checkbox"
            checked={shown}
            onChange={(e) => setShown(e.target.checked)}
          />
          {t("password.show")}
        </label>
      </div>
      {password && again && password !== again && (
        <p className="warn-text small">{t("password.mismatch")}</p>
      )}
      {password && password.length < 8 && (
        <p className="warn-text small">{t("password.tooShort")}</p>
      )}
      <label className="inline">
        <input
          type="checkbox"
          checked={mustChange}
          onChange={(e) => setMustChange(e.target.checked)}
        />
        {t("password.mustChange")}
      </label>
      <NoticeBar notice={notice} />
      <div className="actions">
        <button type="submit" disabled={busy || !valid}>
          {t("password.submit")}
        </button>
        <button type="button" className="secondary" onClick={onCancel}>
          {t("cancel")}
        </button>
      </div>
    </form>
  );
}
