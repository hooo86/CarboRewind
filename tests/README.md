# Tests d'intégration et de bout en bout

Les tests unitaires vivent avec chaque composant (`backend/tests`, `agent/tests`, `frontend/src`).

Ce dossier accueillera (M2 → M5) :

- `integration/` : API + PostgreSQL + Redis réels (docker compose), agent contre un **simulateur
  SOAP/REST Carbonio** (faux serveur reproduisant `SyncRequest`, `AddMsgRequest`, REST `?id=`).
- `e2e/` : Playwright sur l'interface (sauvegarde → restauration d'un mail).
- `carbonio-lab/` : procédure de validation sur un **vrai** Carbonio CE de test (aller-retour
  d'un compte à l'identique), exécutée manuellement ou sur un runner auto-hébergé.
