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
