import { useState } from "react";
import type { FormEvent } from "react";
import { useTranslation } from "react-i18next";

import { api } from "../api/endpoints";
import type {
  ShareInput,
  ShareSettings,
  StorageReport,
  StorageSettings,
} from "../api/types";
import { formatBytes, formatDate } from "../utils/format";
import { LoadView, StatusBadge, errorMessage, useLoad } from "./common";

type ShareForm = ShareInput & { password: string };

function toForm(s: ShareSettings | null): ShareForm {
  return {
    share: s?.share ?? "",
    username: s?.username ?? "",
    domain: s?.domain ?? "",
    seal: s?.seal ?? false,
    password: "",
  };
}

function ShareFields({
  id,
  value,
  stored,
  onChange,
}: {
  id: string;
  value: ShareForm;
  stored: ShareSettings | null;
  onChange: (v: ShareForm) => void;
}) {
  const { t } = useTranslation();
  const set = <K extends keyof ShareForm>(k: K, v: ShareForm[K]) =>
    onChange({ ...value, [k]: v });
  return (
    <>
      <div className="row">
        <label className="grow">
          {t("storage.share")}
          <input
            id={`${id}-share`}
            required
            placeholder="//nas.example.org/sauvegardes/carborewind"
            value={value.share}
            onChange={(e) => set("share", e.target.value)}
          />
        </label>
      </div>
      <div className="row">
        <label className="grow">
          {t("storage.username")}
          <input
            required
            autoComplete="off"
            value={value.username}
            onChange={(e) => set("username", e.target.value)}
          />
        </label>
        <label className="grow">
          {t("storage.password")}
          <input
            type="password"
            autoComplete="new-password"
            required={!stored?.password_set}
            placeholder={
              stored?.password_set ? t("storage.passwordKept") : undefined
            }
            value={value.password}
            onChange={(e) => set("password", e.target.value)}
          />
        </label>
        <label>
          {t("storage.domain")}
          <input
            value={value.domain}
            placeholder="WORKGROUP"
            onChange={(e) => set("domain", e.target.value)}
          />
        </label>
      </div>
      <label className="inline">
        <input
          type="checkbox"
          checked={value.seal}
          onChange={(e) => set("seal", e.target.checked)}
        />
        {t("storage.seal")}
      </label>
    </>
  );
}

function Place({
  label,
  place,
}: {
  label: string;
  place: Partial<StorageReport> | null | undefined;
}) {
  const { t } = useTranslation();
  if (!place?.path) return null;
  return (
    <li>
      <strong>{label}</strong> :{" "}
      {place.kind === "network" ? (
        <>
          <code>{place.source}</code> ({place.fstype})
        </>
      ) : (
        <>
          <code>{place.path}</code> ({t("storage.localDisk")})
        </>
      )}
      {place.error ? (
        <span className="bad-text"> — {t("storage.unreachable")}</span>
      ) : (
        place.total_bytes != null && (
          <>
            {" — "}
            {t("dashboard.storageFree", {
              free: formatBytes(place.free_bytes ?? 0),
              total: formatBytes(place.total_bytes),
            })}
          </>
        )
      )}
    </li>
  );
}

function Status({
  s,
  onReplicate,
}: {
  s: StorageSettings;
  onReplicate: () => void;
}) {
  const { t } = useTranslation();
  return (
    <div className="card stack" aria-label={t("storage.status")}>
      <h3>{t("storage.status")}</h3>
      {s.revision > 0 && !s.applied && (
        <p className="alert info">{t("storage.pending")}</p>
      )}
      {s.agents.length === 0 && <p className="muted">{t("storage.noAgent")}</p>}
      {s.agents.map((a) => (
        <div key={a.agent}>
          <p>
            {t("storage.agent", { agent: a.agent })}
            {a.apply?.at && (
              <>
                {" "}
                ·{" "}
                {t("storage.appliedAt", {
                  when: formatDate(a.apply.at),
                })}{" "}
                {a.apply.ok === false ? (
                  <span className="badge bad">{t("storage.applyFailed")}</span>
                ) : (
                  <span className="badge ok">{t("storage.applyOk")}</span>
                )}
              </>
            )}
          </p>
          {a.apply?.ok === false && a.apply.error && (
            <p className="alert error">{a.apply.error}</p>
          )}
          <ul>
            <Place label={t("storage.copy1")} place={a} />
            <Place label={t("storage.copy2")} place={a.replica} />
          </ul>
        </div>
      ))}
      {s.copy_02 && (
        <div className="toolbar">
          <span className="grow">
            {s.last_replication ? (
              <>
                {t("storage.lastReplication")}{" "}
                <StatusBadge status={s.last_replication.status} />{" "}
                {formatDate(
                  s.last_replication.finished_at ??
                    s.last_replication.created_at,
                )}
                {s.last_replication.progress && (
                  <>
                    {" — "}
                    {t("storage.replicated", {
                      copied: Number(s.last_replication.progress.copied ?? 0),
                      size: formatBytes(
                        Number(s.last_replication.progress.copied_bytes ?? 0),
                      ),
                      deleted: Number(s.last_replication.progress.deleted ?? 0),
                    })}
                  </>
                )}
                {s.last_replication.error && (
                  <span className="bad-text">
                    {" "}
                    — {s.last_replication.error}
                  </span>
                )}
              </>
            ) : (
              <span className="muted">{t("storage.neverReplicated")}</span>
            )}
          </span>
          <button type="button" className="secondary" onClick={onReplicate}>
            {t("storage.replicateNow")}
          </button>
        </div>
      )}
    </div>
  );
}

function Form({
  initial,
  onSaved,
}: {
  initial: StorageSettings;
  onSaved: (s: StorageSettings) => void;
}) {
  const { t } = useTranslation();
  const [kind, setKind] = useState<"local" | "smb">(
    initial.primary_kind ?? "local",
  );
  const [primary, setPrimary] = useState(() => toForm(initial.primary_smb));
  const [migrate, setMigrate] = useState(initial.migrate);
  const [copyOn, setCopyOn] = useState(initial.copy_02 !== null);
  const [copy, setCopy] = useState(() => toForm(initial.copy_02));
  const [message, setMessage] = useState<{
    kind: "ok" | "error";
    text: string;
  } | null>(null);
  const [busy, setBusy] = useState(false);

  const share = (f: ShareForm): ShareInput => ({
    share: f.share.trim(),
    username: f.username.trim(),
    domain: f.domain.trim(),
    seal: f.seal,
    password: f.password || null,
  });

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setMessage(null);
    try {
      const saved = await api.saveStorageSettings({
        primary: {
          kind,
          smb: kind === "smb" ? share(primary) : null,
          migrate,
        },
        copy_02: copyOn ? share(copy) : null,
      });
      setPrimary((p) => ({ ...p, password: "" }));
      setCopy((c) => ({ ...c, password: "" }));
      setMessage({ kind: "ok", text: t("storage.saved") });
      onSaved(saved);
    } catch (err) {
      setMessage({
        kind: "error",
        text: t("error", { message: errorMessage(err) }),
      });
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="card stack" onSubmit={(e) => void submit(e)}>
      {!initial.secret_key_configured && (
        <p className="alert warn">{t("settings.noSecretKey")}</p>
      )}
      <fieldset>
        <legend>{t("storage.copy1")}</legend>
        <p className="muted">{t("storage.copy1Help")}</p>
        <div className="row">
          <label className="inline">
            <input
              type="radio"
              name="primary-kind"
              checked={kind === "local"}
              onChange={() => setKind("local")}
            />
            {t("storage.local")}
          </label>
          <label className="inline">
            <input
              type="radio"
              name="primary-kind"
              checked={kind === "smb"}
              onChange={() => setKind("smb")}
            />
            {t("storage.nas")}
          </label>
        </div>
        {kind === "smb" && (
          <ShareFields
            id="primary"
            value={primary}
            stored={initial.primary_smb}
            onChange={setPrimary}
          />
        )}
        <label className="inline">
          <input
            type="checkbox"
            checked={migrate}
            onChange={(e) => setMigrate(e.target.checked)}
          />
          {t("storage.migrate")}
        </label>
        {!migrate && <p className="alert warn">{t("storage.startEmpty")}</p>}
      </fieldset>
      <fieldset>
        <legend>{t("storage.copy2")}</legend>
        <p className="muted">{t("storage.copy2Help")}</p>
        <label className="inline">
          <input
            type="checkbox"
            checked={copyOn}
            onChange={(e) => setCopyOn(e.target.checked)}
          />
          {t("storage.copy2Enable")}
        </label>
        {copyOn && (
          <ShareFields
            id="copy"
            value={copy}
            stored={initial.copy_02}
            onChange={setCopy}
          />
        )}
      </fieldset>
      {message && (
        <p
          role={message.kind === "error" ? "alert" : "status"}
          className={`alert ${message.kind}`}
        >
          {message.text}
        </p>
      )}
      <div className="actions">
        <button type="submit" disabled={busy}>
          {t("storage.save")}
        </button>
      </div>
    </form>
  );
}

export function StorageSettingsSection() {
  const { t } = useTranslation();
  const [load, reload] = useLoad<StorageSettings>(
    () => api.storageSettings(),
    [],
    15000,
  );
  const [saved, setSaved] = useState<StorageSettings | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  return (
    <section aria-labelledby="storage-title">
      <h2 id="storage-title">{t("storage.title")}</h2>
      <LoadView load={load} reload={reload}>
        {(s) => {
          const current = saved && saved.revision > s.revision ? saved : s;
          return (
            <>
              <Form
                initial={current}
                onSaved={(n) => {
                  setSaved(n);
                  reload();
                }}
              />
              {notice && (
                <p role="status" className="alert ok">
                  {notice}
                </p>
              )}
              <Status
                s={current}
                onReplicate={() =>
                  void api
                    .replicateNow()
                    .then(() => {
                      setNotice(t("storage.replicateQueued"));
                      reload();
                    })
                    .catch((err: unknown) =>
                      setNotice(t("error", { message: errorMessage(err) })),
                    )
                }
              />
            </>
          );
        }}
      </LoadView>
    </section>
  );
}
