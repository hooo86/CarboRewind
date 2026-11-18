import i18n from "i18next";
import { initReactI18next } from "react-i18next";

export const resources = {
  fr: {
    translation: {
      tagline: "Sauvegarde et restauration point-in-time pour Carbonio",
      language: "Langue",
      loading: "Chargement…",
      error: "Erreur : {{message}}",
      retry: "Réessayer",
      nav: { dashboard: "Tableau de bord", accounts: "Comptes", jobs: "Jobs" },
      login: {
        title: "Connexion",
        username: "Identifiant",
        password: "Mot de passe",
        submit: "Se connecter",
        submitting: "Connexion…",
        invalid: "Identifiants invalides.",
        throttled: "Trop de tentatives, réessayez plus tard.",
        failed: "Connexion impossible ({{message}}).",
      },
      logout: "Se déconnecter",
      dashboard: {
        title: "Tableau de bord",
        accounts: "Comptes",
        protected: "Sauvegardés < 24 h",
        never: "Jamais sauvegardés",
        partial: "Dernière sauvegarde partielle",
        queued: "Jobs en attente",
        running: "Jobs en cours",
        failed: "Échecs < 24 h",
        stored: "Volume stocké",
        agents: "Agents actifs",
        heartbeat: "Dernier signal agent : {{when}}",
        recentJobs: "Jobs récents",
      },
      accounts: {
        title: "Comptes",
        domain: "Domaine",
        search: "Rechercher",
        searchPlaceholder: "adresse ou partie d'adresse",
        noDomain:
          "Aucun domaine inventorié. L'agent doit être enrôlé et démarré.",
        empty: "Aucun compte.",
        email: "Compte",
        status: "État",
        lastBackup: "Dernière sauvegarde",
        never: "jamais",
        actions: "Actions",
        backupIncremental: "Incrémentale",
        backupFull: "Complète",
        backupTitle: "Lancer une sauvegarde {{type}} de {{email}}",
        queued: "Sauvegarde {{type}} de {{email}} mise en file.",
        count: "{{shown}} sur {{total}}",
        previous: "Précédent",
        next: "Suivant",
      },
      account: {
        back: "← Comptes",
        domain: "Domaine",
        status: "État",
        lastSeen: "Vu sur Carbonio",
        snapshotCount: "Snapshots",
        backupNow: "Sauvegarder maintenant",
        snapshots: "Historique des snapshots",
        noSnapshot: "Aucun snapshot pour ce compte.",
        seq: "N°",
        takenAt: "Date",
        type: "Type",
        items: "Éléments",
        size: "Taille",
        newBytes: "Nouvelles données",
        jobs: "Jobs de ce compte",
      },
      jobs: {
        title: "Jobs",
        filter: "Filtrer",
        all: "Tous",
        empty: "Aucun job.",
        created: "Créé",
        account: "Compte",
        kind: "Type",
        status: "État",
        duration: "Durée",
        attempts: "Essais",
        error: "Erreur",
        autoRefresh: "Actualisation automatique toutes les 5 s",
      },
      status: {
        active: "actif",
        locked: "verrouillé",
        closed: "fermé",
        deleted: "supprimé",
        complete: "complet",
        partial: "partiel",
        expired: "expiré",
        queued: "en attente",
        running: "en cours",
        succeeded: "réussi",
        failed: "échec",
        cancelled: "annulé",
      },
      backupType: { full: "complète", incremental: "incrémentale" },
      kind: {
        backup: "sauvegarde",
        restore: "restauration",
        config_backup: "sauvegarde config",
        verify: "vérification",
        purge: "purge",
      },
    },
  },
  en: {
    translation: {
      tagline: "Point-in-time backup and restore for Carbonio",
      language: "Language",
      loading: "Loading…",
      error: "Error: {{message}}",
      retry: "Retry",
      nav: { dashboard: "Dashboard", accounts: "Accounts", jobs: "Jobs" },
      login: {
        title: "Sign in",
        username: "Username",
        password: "Password",
        submit: "Sign in",
        submitting: "Signing in…",
        invalid: "Invalid credentials.",
        throttled: "Too many attempts, try again later.",
        failed: "Sign-in failed ({{message}}).",
      },
      logout: "Sign out",
      dashboard: {
        title: "Dashboard",
        accounts: "Accounts",
        protected: "Backed up < 24 h",
        never: "Never backed up",
        partial: "Last backup partial",
        queued: "Queued jobs",
        running: "Running jobs",
        failed: "Failures < 24 h",
        stored: "Stored data",
        agents: "Active agents",
        heartbeat: "Last agent heartbeat: {{when}}",
        recentJobs: "Recent jobs",
      },
      accounts: {
        title: "Accounts",
        domain: "Domain",
        search: "Search",
        searchPlaceholder: "address or part of it",
        noDomain:
          "No domain inventoried yet. The agent must be enrolled and running.",
        empty: "No account.",
        email: "Account",
        status: "Status",
        lastBackup: "Last backup",
        never: "never",
        actions: "Actions",
        backupIncremental: "Incremental",
        backupFull: "Full",
        backupTitle: "Start a {{type}} backup of {{email}}",
        queued: "{{type}} backup of {{email}} queued.",
        count: "{{shown}} of {{total}}",
        previous: "Previous",
        next: "Next",
      },
      account: {
        back: "← Accounts",
        domain: "Domain",
        status: "Status",
        lastSeen: "Seen on Carbonio",
        snapshotCount: "Snapshots",
        backupNow: "Back up now",
        snapshots: "Snapshot history",
        noSnapshot: "No snapshot for this account.",
        seq: "#",
        takenAt: "Date",
        type: "Type",
        items: "Items",
        size: "Size",
        newBytes: "New data",
        jobs: "Jobs for this account",
      },
      jobs: {
        title: "Jobs",
        filter: "Filter",
        all: "All",
        empty: "No job.",
        created: "Created",
        account: "Account",
        kind: "Kind",
        status: "Status",
        duration: "Duration",
        attempts: "Attempts",
        error: "Error",
        autoRefresh: "Refreshes every 5 s",
      },
      status: {
        active: "active",
        locked: "locked",
        closed: "closed",
        deleted: "deleted",
        complete: "complete",
        partial: "partial",
        expired: "expired",
        queued: "queued",
        running: "running",
        succeeded: "succeeded",
        failed: "failed",
        cancelled: "cancelled",
      },
      backupType: { full: "full", incremental: "incremental" },
      kind: {
        backup: "backup",
        restore: "restore",
        config_backup: "config backup",
        verify: "verify",
        purge: "purge",
      },
    },
  },
} as const;

export type Language = keyof typeof resources;

function detectLanguage(): Language {
  try {
    const saved = localStorage.getItem("carborewind.lang");
    if (saved === "fr" || saved === "en") return saved;
  } catch {
    /* storage unavailable */
  }
  return navigator.language.toLowerCase().startsWith("fr") ? "fr" : "en";
}

export function setLanguage(lang: Language): void {
  try {
    localStorage.setItem("carborewind.lang", lang);
  } catch {
    /* storage unavailable */
  }
  void i18n.changeLanguage(lang);
}

void i18n.use(initReactI18next).init({
  resources,
  lng: detectLanguage(),
  fallbackLng: "en",
  interpolation: { escapeValue: false }, // React already escapes output
});

export default i18n;
