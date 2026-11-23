import { useState } from "react";
import type { FormEvent } from "react";
import { useTranslation } from "react-i18next";

import { api } from "../api/endpoints";
import type { Blocklist } from "../api/types";
import { formatDate } from "../utils/format";
import { LoadView, errorMessage, useLoad } from "./common";
import { NoticeBar, type Notice } from "./notices";

/** Shared by millions of legitimate senders: offering to block them would be a trap. */
export const PROVIDERS = new Set([
  "outlook.com",
  "hotmail.com",
  "hotmail.fr",
  "live.com",
  "live.fr",
  "msn.com",
  "gmail.com",
  "googlemail.com",
  "yahoo.com",
  "yahoo.fr",
  "icloud.com",
  "me.com",
  "aol.com",
  "gmx.com",
  "gmx.fr",
  "orange.fr",
  "free.fr",
  "laposte.net",
  "protonmail.com",
  "proton.me",
]);

/** "Block the address" / "Block the domain" for one spam sender. */
export function SenderBlockButtons({
  address,
  blocked,
  onDone,
}: {
  address: string;
  blocked: Set<string>;
  onDone: (notice: Notice) => void;
}) {
  const { t } = useTranslation();
  const [busy, setBusy] = useState(false);
  const domain = address.includes("@") ? (address.split("@").pop() ?? "") : "";
  const block = async (value: string) => {
    setBusy(true);
    try {
      await api.blockSender(value, t("blocklist.fromSpam"));
      onDone({ kind: "ok", text: t("blocklist.blocked", { value }) });
    } catch (err) {
      onDone({
        kind: "error",
        text: t("error", { message: errorMessage(err) }),
      });
    } finally {
      setBusy(false);
    }
  };
  if (!address.includes("@")) return null;
  if (blocked.has(address) || blocked.has(`@${domain}`))
    return <span className="muted small">{t("blocklist.isBlocked")}</span>;
  return (
    <span className="actions-inline">
      <button
        type="button"
        className="link small"
        disabled={busy}
        onClick={() => void block(address)}
      >
        {t("blocklist.blockAddress")}
      </button>
      {!PROVIDERS.has(domain) && (
        <button
          type="button"
          className="link small"
          disabled={busy}
          onClick={() => void block(`@${domain}`)}
        >
          {t("blocklist.blockDomain", { domain })}
        </button>
      )}
    </span>
  );
}

/** Trafic mail: the blocked senders, applied by the agent in Carbonio (Amavis). */
export function BlocklistPanel({ refresh = 0 }: { refresh?: number }) {
  const { t } = useTranslation();
  const [load, reload] = useLoad<Blocklist>(
    () => api.blocklist(),
    [refresh],
    60000,
  );
  const [value, setValue] = useState("");
  const [reason, setReason] = useState("");
  const [force, setForce] = useState(false);
  const [notice, setNotice] = useState<Notice>(null);
  const [busy, setBusy] = useState(false);

  async function run(action: () => Promise<unknown>, ok: string) {
    setBusy(true);
    setNotice(null);
    try {
      await action();
      setNotice({ kind: "ok", text: ok });
      reload();
    } catch (err) {
      setNotice({
        kind: "error",
        text: t("error", { message: errorMessage(err) }),
      });
    } finally {
      setBusy(false);
    }
  }

  const add = (event: FormEvent) => {
    event.preventDefault();
    const v = value.trim();
    if (!v) return;
    void run(
      async () => {
        await api.blockSender(v, reason.trim(), force);
        setValue("");
        setReason("");
        setForce(false);
      },
      t("blocklist.blocked", { value: v }),
    );
  };

  const domain = value.trim().toLowerCase().replace(/^.*@/, "");
  return (
    <LoadView load={load} reload={reload}>
      {(d) => (
        <div className="stack">
          <p className="muted small">{t("blocklist.help")}</p>
          <form className="row" onSubmit={add} aria-label={t("blocklist.add")}>
            <label className="grow">
              {t("blocklist.value")}
              <input
                value={value}
                placeholder="spammeur@exemple.com ou exemple.com"
                onChange={(e) => setValue(e.target.value)}
              />
            </label>
            <label className="grow">
              {t("blocklist.reason")}
              <input
                value={reason}
                maxLength={300}
                onChange={(e) => setReason(e.target.value)}
              />
            </label>
            {!value.includes("@") || value.trim().startsWith("@")
              ? PROVIDERS.has(domain) && (
                  <label className="inline">
                    <input
                      type="checkbox"
                      checked={force}
                      onChange={(e) => setForce(e.target.checked)}
                    />
                    {t("blocklist.force", { domain })}
                  </label>
                )
              : null}
            <button type="submit" disabled={busy || !value.trim()}>
              {t("blocklist.add")}
            </button>
          </form>
          <NoticeBar notice={notice} />
          {d.entries.length === 0 ? (
            <p className="muted">{t("blocklist.empty")}</p>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>{t("blocklist.value")}</th>
                    <th>{t("blocklist.reason")}</th>
                    <th>{t("blocklist.by")}</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {d.entries.map((e) => (
                    <tr key={e.value}>
                      <td>
                        <code>{e.value}</code>{" "}
                        <span className="muted small">
                          {t(`blocklist.kind.${e.kind}`)}
                        </span>
                      </td>
                      <td className="small">{e.reason ?? "—"}</td>
                      <td className="small">
                        {e.by} — {formatDate(e.at)}
                      </td>
                      <td>
                        <button
                          type="button"
                          className="small secondary"
                          disabled={busy}
                          onClick={() =>
                            void run(
                              () => api.unblockSender(e.value),
                              t("blocklist.unblocked", { value: e.value }),
                            )
                          }
                        >
                          {t("blocklist.unblock")}
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {d.revision > 0 &&
            d.agents.map((a) => (
              <p
                key={a.name}
                className={a.status?.error ? "bad-text small" : "muted small"}
              >
                {a.applied
                  ? t("blocklist.applied", {
                      agent: a.name,
                      domains: a.status?.domains.join(", ") || "—",
                    })
                  : a.status?.error
                    ? t("blocklist.failed", {
                        agent: a.name,
                        error: a.status.error,
                      })
                    : t("blocklist.pending", { agent: a.name })}
              </p>
            ))}
        </div>
      )}
    </LoadView>
  );
}
