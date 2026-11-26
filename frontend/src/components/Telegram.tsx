import { useState } from "react";
import type { FormEvent } from "react";
import { useTranslation } from "react-i18next";

import { api } from "../api/endpoints";
import type { TelegramSettings } from "../api/types";
import { errorMessage, useLoad } from "./common";
import { NoticeBar, type Notice } from "./notices";

/** Settings → Notifications → Telegram: bot, chats, which alerts. */
export function TelegramForm({
  initial,
  events,
  onSaved,
}: {
  initial: TelegramSettings;
  events: readonly string[];
  onSaved: () => void;
}) {
  const { t } = useTranslation();
  const [enabled, setEnabled] = useState(initial.enabled);
  const [token, setToken] = useState("");
  const [changeToken, setChangeToken] = useState(!initial.token_set);
  const [chats, setChats] = useState(initial.chat_ids.join("\n"));
  const [wanted, setWanted] = useState(initial.events);
  const [found, setFound] = useState<
    { id: string; title: string; type: string }[]
  >([]);
  const [notice, setNotice] = useState<Notice>(null);
  const [busy, setBusy] = useState(false);
  const chatList = chats
    .split(/[\s,;]+/)
    .map((c) => c.trim())
    .filter(Boolean);

  async function run(action: () => Promise<void>) {
    setBusy(true);
    setNotice(null);
    try {
      await action();
    } catch (err) {
      setNotice({
        kind: "error",
        text: t("error", { message: errorMessage(err) }),
      });
    } finally {
      setBusy(false);
    }
  }

  const save = (event: FormEvent) => {
    event.preventDefault();
    void run(async () => {
      await api.saveTelegram({
        enabled,
        token: changeToken ? token.trim() || null : null,
        chat_ids: chatList,
        events: wanted,
      });
      setToken("");
      setChangeToken(false);
      setNotice({ kind: "ok", text: t("telegram.saved") });
      onSaved();
    });
  };

  return (
    <form
      className="card stack"
      onSubmit={save}
      aria-label={t("telegram.title")}
    >
      <h2>{t("telegram.title")}</h2>
      <p className="muted">{t("telegram.intro")}</p>
      <ol className="muted small">
        <li>{t("telegram.step1")}</li>
        <li>{t("telegram.step2")}</li>
        <li>{t("telegram.step3")}</li>
      </ol>
      <label className="check">
        <input
          type="checkbox"
          checked={enabled}
          onChange={(e) => setEnabled(e.target.checked)}
        />
        {t("telegram.enabled")}
      </label>
      <div className="row">
        <label className="grow">
          {t("telegram.token")}
          {changeToken ? (
            <input
              type="password"
              autoComplete="off"
              value={token}
              placeholder="123456789:AA…"
              onChange={(e) => setToken(e.target.value)}
            />
          ) : (
            <span className="muted">{t("settings.passwordStored")}</span>
          )}
        </label>
        {!changeToken && (
          <div className="actions">
            <button
              type="button"
              className="small secondary"
              onClick={() => setChangeToken(true)}
            >
              {t("telegram.changeToken")}
            </button>
          </div>
        )}
      </div>
      <div className="row">
        <label className="grow">
          {t("telegram.chats")}
          <textarea
            rows={2}
            value={chats}
            placeholder="-1001234567890"
            onChange={(e) => setChats(e.target.value)}
          />
        </label>
        <div className="actions">
          <button
            type="button"
            className="small secondary"
            disabled={busy || !initial.token_set}
            onClick={() =>
              void run(async () => {
                const list = await api.telegramChats();
                setFound(list);
                if (list.length === 0)
                  setNotice({ kind: "error", text: t("telegram.noChat") });
              })
            }
          >
            {t("telegram.discover")}
          </button>
        </div>
      </div>
      {found.length > 0 && (
        <ul className="stack small" aria-label={t("telegram.found")}>
          {found.map((c) => (
            <li key={c.id}>
              <button
                type="button"
                className="link"
                disabled={chatList.includes(c.id)}
                onClick={() =>
                  setChats((v) => (v.trim() ? `${v.trim()}\n${c.id}` : c.id))
                }
              >
                {t("telegram.add", { title: c.title, id: c.id })}
              </button>
            </li>
          ))}
        </ul>
      )}
      <fieldset>
        <legend>{t("telegram.events")}</legend>
        {events.map((ev) => (
          <label key={ev} className="check">
            <input
              type="checkbox"
              checked={wanted[ev] ?? false}
              onChange={(e) =>
                setWanted((w) => ({ ...w, [ev]: e.target.checked }))
              }
            />
            {t(`settings.eventNames.${ev}`)}
          </label>
        ))}
      </fieldset>
      <NoticeBar notice={notice} />
      <div className="actions">
        <button type="submit" disabled={busy}>
          {t("telegram.save")}
        </button>
        <button
          type="button"
          className="secondary"
          disabled={busy || !initial.enabled}
          onClick={() =>
            void run(async () => {
              const r = await api.testTelegram();
              setNotice(
                r.ok
                  ? { kind: "ok", text: t("telegram.testOk") }
                  : {
                      kind: "error",
                      text: t("settings.testFailed", { error: r.error }),
                    },
              );
            })
          }
        >
          {t("telegram.test")}
        </button>
      </div>
    </form>
  );
}

export function TelegramSection({ events }: { events: readonly string[] }) {
  const [load, reload] = useLoad(() => api.telegram(), []);
  if (load.state !== "ready") return null; // an older server: no Telegram section
  const cfg = load.data;
  return (
    <TelegramForm
      key={`${cfg.token_set}-${cfg.chat_ids.join()}-${cfg.enabled}`}
      initial={cfg}
      events={events}
      onSaved={reload}
    />
  );
}
