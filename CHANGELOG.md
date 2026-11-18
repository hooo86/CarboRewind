# Changelog

Toutes les évolutions notables de ce projet sont consignées ici.
Format : [Keep a Changelog](https://keepachangelog.com/fr/1.1.0/) · Versionnage : [SemVer](https://semver.org/lang/fr/).

## [Unreleased]

### Added
- Initialisation du dépôt : licence AGPL-3.0, README FR/EN, guides de contribution et de sécurité.
- Monorepo : squelettes `backend` (FastAPI, en-têtes de sécurité, journaux JSON, `/metrics`),
  `agent` (configuration, `check-config`), `frontend` (React + TypeScript, i18n FR/EN).
- CI GitHub Actions : lint, typage, tests (couverture ≥ 80 %), build Docker, gitleaks,
  dependency review, CodeQL ; release sur tag avec publication ghcr.io ; Dependabot.
- Templates d'issues et de PR, script `scripts/github-bootstrap.sh` (labels, jalons, protections).
- Documentation d'architecture et feuille de route.
- Modèle de données PostgreSQL (SQLAlchemy 2, migrations Alembic) : identités et RBAC,
  inventaire Carbonio, agents, politiques, jobs (bail, checkpoint, idempotence), snapshots,
  catalogue temporel (`item_versions` / `folder_versions`), blobs, audit append-only.
- `/readyz` vérifie PostgreSQL et Redis (503 sans divulgation des erreurs).
- `docs/data-model.md` (diagramme ER) et `docs/schema.sql` généré et vérifié en test.
- Authentification : comptes locaux (Argon2id) et LDAP Carbonio (TLS obligatoire, filtre échappé,
  provisionnement sans rôle), sessions serveur (`__Host-` cookies, inactivité 30 min, absolue 12 h),
  CSRF double submit, limitation par IP, verrouillage exponentiel, réponses génériques.
- Journal d'audit chaîné (SHA-256) alimenté par les connexions / déconnexions.
- CLI `carborewind-api create-user` pour créer le premier super-admin.
