import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { fetchHealth, type Health } from "./api";
import type { Language } from "./i18n";

type ApiState = { kind: "checking" } | { kind: "up"; health: Health } | { kind: "down" };

export function App() {
  const { t, i18n } = useTranslation();
  const [api, setApi] = useState<ApiState>({ kind: "checking" });

  useEffect(() => {
    const controller = new AbortController();
    fetchHealth(controller.signal)
      .then((health) => setApi({ kind: "up", health }))
      .catch(() => {
        if (!controller.signal.aborted) setApi({ kind: "down" });
      });
    return () => controller.abort();
  }, []);

  const status =
    api.kind === "checking"
      ? t("checking")
      : api.kind === "up"
        ? t("up", { version: api.health.version })
        : t("down");

  return (
    <main>
      <header>
        <h1>CarboRewind</h1>
        <p>{t("tagline")}</p>
        <label>
          {t("language")}{" "}
          <select
            value={i18n.language}
            onChange={(e) => void i18n.changeLanguage(e.target.value as Language)}
          >
            <option value="fr">Français</option>
            <option value="en">English</option>
          </select>
        </label>
      </header>
      <section aria-live="polite">
        <h2>{t("apiStatus")}</h2>
        <p data-testid="api-status">{status}</p>
      </section>
    </main>
  );
}
