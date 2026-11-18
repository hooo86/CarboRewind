# Changelog

Toutes les évolutions notables de ce projet sont consignées ici.
Format : [Keep a Changelog](https://keepachangelog.com/fr/1.1.0/) · Versionnage : [SemVer](https://semver.org/lang/fr/).

## [Unreleased]

### Added
- Liste des comptes : détails Carbonio (nom, prénom, alias, classe de service, quota, espace utilisé, admin,
  dernière connexion, création) et de sauvegarde ; choix des colonnes ; filtres à la Odoo (prédéfinis et
  personnalisés sur tous les champs) ; tri ; sélection multiple → sauvegarde groupée ou politique sur la
  sélection (`target_type = accounts`).
- Notifications SMTP : paramètres chiffrés, e-mail de test, file d'envoi avec reprises, alertes (échec,
  sauvegarde partielle, restauration incomplète, agent silencieux, résumé quotidien).
- Stockage du dépôt sur partage SMB/CIFS : `carborewind-agent storage smb|local|status`, montage systemd,
  migration du dépôt existant, espace libre au tableau de bord. Migrations 0005 et 0006.
- Planification : politiques de sauvegarde (cron + fuseau, portée globale / domaine / compte), service
  `scheduler`, rétention grand-père/père/fils, purge du dépôt par l'agent, écran **Politiques**, alertes du
  tableau de bord (comptes non protégés depuis 24 h, aucune politique). Migration 0004.
- Restauration point-in-time (agent, API, interface) : compte entier, dossiers ou éléments ; nouveau
  dossier daté ou dossiers d'origine sans doublon ; même compte ou autre compte ; simulation (dry-run) ;
  reprise sur incident ; catalogue d'un snapshot consultable (arborescence, recherche) ; audit complet.
- Interface web (React Router) : connexion locale/LDAP avec CSRF, tableau de bord, comptes par domaine
  (recherche, pagination, dernière sauvegarde), « Sauvegarder maintenant » incrémentale/complète selon le
  rôle, suivi des jobs (filtre, rafraîchissement 5 s), fiche compte avec historique des snapshots ; FR/EN,
  thème clair/sombre.
- CLI `carborewind-api set-password` (réinitialise, déverrouille, ferme les sessions, audité) ;
  `create-user` sur un nom existant affiche un message clair au lieu d'une trace.
- API : `GET /api/dashboard`, `GET /api/accounts/{id}`, `GET /api/accounts/{id}/snapshots`, `GET /api/jobs` ;
  dernière sauvegarde dans la liste des comptes.
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
- RBAC : matrice permissions × rôles (`super_admin`, `domain_admin`, `restore_operator`,
  `viewer`) avec portée par domaine, refus audités (`authz.denied`).
- API d'administration des utilisateurs et des rôles (garde-fou du dernier super-admin,
  révocation des sessions au retrait de rôle) et inventaire filtré par domaine.
- `docs/openapi.json` et `docs/rbac.md` générés depuis le code et vérifiés en test.
- Audit : consultation filtrée et limitée par domaine, export CSV en flux (protégé contre
  l'injection de formules), vérification de la chaîne (`/api/audit/verify`, CLI
  `audit-verify`), tête de chaîne à ancrer hors base (`/api/audit/head`).
- `deploy/sql/grants.sql` : rôle applicatif sans DDL et en ajout seul sur l'audit.
- Agent : dépôt de sauvegarde chiffré (AES-256-GCM, KEK → clé maître → sous-clés HKDF),
  compressé (zstd), dédupliqué (nom HMAC), écritures atomiques et idempotentes, AAD
  anti-substitution, verrous, vérification complète ou échantillonnée, rotation de la KEK ;
  commandes `carborewind-agent repo keygen|init|verify|rotate-key`.
- Agent : client Carbonio CE (SOAP JSON admin et mail, REST) — authentification admin,
  délégation par compte, inventaire paginé, `SyncRequest` incrémental avec `MUST_RESYNC`,
  lecture brute et métadonnées, primitives de restauration ; reprise sur erreurs transitoires ;
  TLS toujours vérifié ; simulateur Carbonio pour les tests ; commande en lecture seule
  `carborewind-agent carbonio probe` pour vérifier les hypothèses d'API sur un vrai serveur.
- Agent : moteur de sauvegarde à chaud par compte — complète ou incrémentale (`SyncRequest`),
  manifests complets (point-in-time), bascule automatique sur `MUST_RESYNC`, checkpoints et
  reprise sans relecture, snapshot partiel conservant la version précédente en cas d'erreur,
  extraction des métadonnées de recherche, limitation de débit et pause sur charge système.
- Protocole agent ↔ API : CA interne (`carborewind-api pki-init`), enrôlement par jeton à
  usage unique et CSR, authentification mTLS via le reverse proxy (en-têtes acceptés du seul
  proxy), révocation, inventaire, jobs avec bail / heartbeat / checkpoint / reprise /
  idempotence, ingestion transactionnelle du catalogue ; `POST /api/backups`,
  `GET /api/jobs/{id}` ; agent : `enroll` et service `run`.
- Test de bout en bout (agent + API + PostgreSQL + dépôt chiffré) exécuté en CI.
- Installables Linux : paquet `carborewind-agent_<version>_amd64.deb` (Python embarqué,
  service systemd durci, utilisateur dédié) et archive `carborewind-server-<version>.tar.gz`
  (Docker Compose de production, nginx TLS + mTLS, `install-server.sh` idempotent),
  construits et attachés aux releases par la CI ; procédure `docs/install.md`.
- CLI serveur `agent-create` et `backup` ; reconnaissance du reverse proxy par secret partagé.

### Fixed
- nginx : la règle générique `/api` masquait `/api/agent/` (priorité des `location` regex) et
  aurait bloqué tous les agents ; corrigé avec `^~`.
- Agent : le certificat client n'était pas présenté par httpx lorsqu'une CA serveur était
  fournie ; contexte TLS désormais construit explicitement.
- Paquet agent : `/etc/carborewind` non inscriptible par l'utilisateur de service.
