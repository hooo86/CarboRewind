# Feuille de route / Roadmap

Chaque jalon correspond à un *milestone* GitHub ; chaque fonctionnalité à une issue `[Mx] …`
et à une branche `feature/*` fusionnée dans `develop` par pull request.

| Jalon | Contenu | Version |
|---|---|---|
| **M0 – Socle** | Fichiers du dépôt, monorepo, CI (lint, tests, CodeQL, gitleaks, dependency review), Dependabot, templates, compose de dev, architecture | — |
| **M1 – Backend cœur** | Schéma SQL + migrations, API OpenAPI, auth locale + LDAP Carbonio, RBAC 4 rôles, audit chaîné | — |
| **M2 – Agent & sauvegarde** | Enrôlement mTLS, client SOAP/REST, dépôt chiffré dédupliqué, full + incrémentale (`SyncRequest`), reprise, throttling | — |
| **M3 – Restauration** | Catalogue, recherche, prévisualisation, restauration compte / dossier / élément, modes, dry-run, compte supprimé | — |
| **M4 – Frontend** | Tableau de bord, jobs temps réel (SSE), explorateur, assistant de restauration, FR/EN, accessibilité | — |
| **M5 – Exploitation** | Planification, rétention + purge, sauvegarde de configuration, notifications, scripts d'installation, docs (install, admin, PRA, runbook), tests E2E | **v0.1.0** |
| v0.2 | OIDC / SAML + MFA, pilotes S3 et SFTP, réplication, rapports CSV / PDF, tests de restauration automatiques | v0.2.0 |
| v0.3 | Différentielles, multi-serveurs (un agent par mailstore), connecteur Carbonio Advanced | v0.3.0 |
