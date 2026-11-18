import i18n from "i18next";
import { initReactI18next } from "react-i18next";

export const resources = {
  fr: {
    translation: {
      tagline: "Sauvegarde et restauration point-in-time pour Carbonio",
      apiStatus: "État de l'API",
      checking: "vérification…",
      up: "opérationnelle (v{{version}})",
      down: "injoignable",
      language: "Langue",
    },
  },
  en: {
    translation: {
      tagline: "Point-in-time backup and restore for Carbonio",
      apiStatus: "API status",
      checking: "checking…",
      up: "up (v{{version}})",
      down: "unreachable",
      language: "Language",
    },
  },
} as const;

export type Language = keyof typeof resources;

function detectLanguage(): Language {
  return navigator.language.toLowerCase().startsWith("fr") ? "fr" : "en";
}

void i18n.use(initReactI18next).init({
  resources,
  lng: detectLanguage(),
  fallbackLng: "en",
  interpolation: { escapeValue: false }, // React already escapes output
});

export default i18n;
