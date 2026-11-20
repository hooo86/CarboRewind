# Changelog

Toutes les évolutions notables de ce projet sont consignées ici.
Format : [Keep a Changelog](https://keepachangelog.com/fr/1.1.0/) · Versionnage : [SemVer](https://semver.org/lang/fr/).

## [Unreleased]

### Added
- IP bloquées : bouton **Débloquer** (l'agent ajoute l'adresse aux adresses de confiance de
  Carbonio, `zimbraHttpThrottleSafeIPs` : elle n'est plus jamais bloquée), « Retirer la
  confiance », liste des adresses de confiance avec ajout d'une adresse ou d'un réseau, règle
  de blocage de Carbonio (N échecs → blocage X minutes) et heure approximative de levée du
  blocage. Filtre « Seulement les IP bloquées ». Migration 0019.
- L'agent indique s'il peut lire `mailbox.log` : message précis (agent trop ancien, droit
  manquant, fichier absent) au lieu de « Aucune donnée reçue ». Quand rien n'est bloqué : « Aucune
  adresse IP bloquée sur la période ».

### Added
- **Tableau de bord personnalisable** (bouton « Personnaliser ») : ajouter, retirer, déplacer
  les widgets, choisir leur largeur (pleine ou demi), leur titre et les données affichées
  (chiffres clés au choix, nombre de lignes, filtres d'état et de type des jobs, période…).
  Widgets : chiffres clés, stockage, sauvegarde manuelle, derniers jobs, sauvegardes
  programmées, IP bloquées, origine du spam, principaux expéditeurs. Disposition propre à
  chaque utilisateur, retour à la disposition par défaut en un clic. Migration 0018.
- **IP bloquées par Carbonio** après des connexions échouées : l'agent lit `mailbox.log`
  (« Access to IP … suspended, for repeated failed login », « authentication failed for … »
  avec l'IP d'origine `oip`) et les échecs SASL du journal MTA. Trafic mail → « Connexions
  échouées et IP bloquées » : IP, nom DNS inverse, état (bloquée, nombre de blocages), échecs
  webmail/IMAP et SMTP, comptes visés. Voir `docs/security-logins.md`.
- **Origine du spam** : adresses IP des serveurs envoyeurs regroupées par le domaine de leur
  nom DNS inverse, avec les domaines d'expéditeur (Trafic mail et widget).

### Added
- Sauvegardes programmées : une même politique peut combiner une sauvegarde incrémentale (par
  ex. chaque nuit) et une **complète** (chaque semaine, chaque mois ou cron) ; quand les deux
  tombent au même moment, seule la complète est lancée. Migration 0017.
- Pour chaque politique : volume protégé (dernières sauvegardes des comptes couverts), données
  écrites, nombre de comptes, résultat de la dernière exécution, et **historique des exécutions**
  (date, planifiée ou manuelle, type, comptes réussis / en échec, durée, volumes lus et écrits).
- Comptes : **regrouper par** un champ comme dans Odoo (domaine, état, classe de service,
  administrateur, politique, état et type de la dernière sauvegarde, dates par mois, société,
  service, fonction, ville…), avec effectif et volumes par groupe, ouverture d'un groupe et
  « voir tous les comptes du groupe ».
- **Favoris** : enregistrer la recherche actuelle (filtres, regroupement, tri) sous un nom,
  personnelle ou partagée avec les autres utilisateurs.

### Added
- Journaux dans l'interface (menu Configuration → Journaux, super-administrateurs) : API et
  planificateur CarboRewind, agent, MTA Carbonio et mailbox.log lus par l'agent ; nombre de lignes,
  niveau, filtre texte, actualisation automatique, téléchargement ; chaque lecture est auditée.
  Voir `docs/logs.md`.
- Débit des sauvegardes (Mo/s et volume lu) dans les listes de jobs.
- Jobs : filtres par type et par compte, tri par colonne, pagination (25 à 200 par page).
- Archives : vue « Par boîte » (messages, volume, période, conservation légale) et restauration
  complète d'une boîte archivée en un clic (dans la boîte d'origine ou une autre, par lots de 5000).

### Fixed
- Une boîte ne peut plus être sauvegardée deux fois en même temps : une nouvelle demande est
  refusée tant qu'une sauvegarde de la boîte est en attente ou en cours (index unique en base,
  migration 0016 qui annule les doublons déjà en file).
- Sauvegarde des grosses boîtes (plus d'environ 11 000 messages) en échec « HTTP 500 » à la fin :
  l'enregistrement du catalogue est désormais fait par lots (limite de 65 535 paramètres de
  PostgreSQL). Les caractères NUL présents dans certains sujets sont retirés.

### Changed
- Le menu « Politiques » s'appelle désormais « Sauvegardes ».

### Added (suite)
- Alertes Telegram (bot gratuit, groupes détectés automatiquement, choix des alertes, test),
  indépendantes de la messagerie. Migration 0015.
- Test de restauration automatique : messages tirés au hasard réellement restaurés dans une boîte
  de test, dossier nettoyé ensuite, alerte en cas d'échec, historique dans la Supervision.
- Serveur SMTP de secours pour les notifications (bascule automatique, test dédié).
- Surveillance des listes noires (DNSBL) des adresses publiques du serveur de messagerie, avec
  alerte à l'inscription et au retrait, tableau dans la Supervision, bandeau sur le tableau de
  bord et métrique Prometheus.
- Paramètres → Agent : boîtes sauvegardées en parallèle, débit, pause selon la charge, garde-fou
  disque, surveillance des services et du journal MTA, appliqués par l'agent sans redémarrage.
  Voir `docs/agent-settings.md`.
- Sauvegardes plus rapides : jobs en parallèle, métadonnées lues par lots de 200, synchronisation
  des répertoires du NAS une fois par point de reprise.
- Conservation légale d'une boîte entière depuis l'écran Archives (tous les dossiers, rien retiré
  de la boîte, jamais supprimée par la durée de conservation jusqu'à sa levée). Migration 0014.
- Interface : menu du compte en haut à droite (rôle, langue, thème Clair / Sombre / Auto, mot de
  passe et double authentification, Paramètres, déconnexion) ; thème clair ; tous les réglages
  regroupés dans Paramètres en onglets (Notifications, Stockage, Supervision, Limites d'envoi,
  Sécurité) ; autocomplétion de la boîte pour la conservation légale ; police plus grande ; plus de
  débordement horizontal des formulaires et tableaux.
- Archivage des e-mails : politiques (domaines, comptes, âge, dossiers exclus, drapeaux, non lus,
  administrateurs), modes simulation / copie seule / archivage complet avec **simulation
  obligatoire** avant toute suppression ; l'agent relit chaque message archivé, l'indexe avant de
  le supprimer et vérifie la copie 2 ; écran **Archives** (recherche, lecture en texte seul,
  téléchargement .eml, restauration), conservation et purge, compte rendu par e-mail. Migration
  0013. Voir `docs/archives.md`.
- Surveillance des services Carbonio (amavis, antispam, mta, mailbox, opendkim…) et des ports
  essentiels chaque minute, avec alerte e-mail quand un service tombe et quand il revient ;
  alerte sur la taille de la file d'attente mail ; bandeau sur le tableau de bord, bloc dans la
  Supervision, métriques Prometheus.
- Tableau de bord : stockage global des e-mails (volume des boîtes, quotas attribués), jauges
  d'occupation du disque du serveur Carbonio et du NAS de sauvegarde (copie 1 et 2) avec la part des
  e-mails sur chacun ; alerte quand une boîte atteint un seuil réglable (% du quota ou taille en Go),
  événement de notification `mailbox_quota`, métrique `carborewind_mailboxes_used_bytes`.
- Logo (favicon, menu, page de connexion, README).
- Double authentification TOTP : enrôlement par QR code, codes de secours, connexion en deux étapes,
  politique « obligatoire pour les administrateurs / pour tous » ; page **Mon compte** (mot de passe,
  2FA). Migration 0012.
- Gestion des comptes d'accès dans l'interface : création (local/LDAP), rôles par domaine,
  réinitialisation du mot de passe et de la 2FA, déblocage, désactivation, suppression.
- Supervision : évolution du stockage (copie 1 et 2) et prévision de saturation, comptes à risque,
  retard de la copie 2, vérification d'intégrité planifiée (job `verify`, échantillon ou complète,
  copie 1 et 2), agents ; alertes e-mail associées ; métriques Prometheus métier sur `/metrics`
  (jeton `CARBOREWIND_METRICS_TOKEN`), règles d'alerte et tableau de bord Grafana dans
  `deploy/monitoring/`. Migration 0011. Voir `docs/monitoring.md`.
- Garde-fous : arrêt des sauvegardes avant de remplir le disque (local : max(20 Gio, 15 %) ; partage :
  2 Gio), copie 2 qui ne remplit pas son partage ; verrouillages automatiques jamais sur un compte
  administrateur ni sur le compte technique de CarboRewind, plafonnés par heure ; mesure de la taille
  des boîtes limitée à 200 appels par inventaire.
- Trafic mail : messages et destinataires envoyés par boîte (par heure et par jour), spams reçus,
  expéditeurs de spams (avec IP), spams émis par une boîte locale, lus dans le journal du MTA par
  l'agent ; limites d'envoi avec alerte e-mail ou verrouillage automatique de la boîte, exemptions,
  verrouillage/déverrouillage manuel. Écran **Trafic mail**, migration 0010. Voir `docs/mail-traffic.md`.
- Stockage depuis l'interface (Paramètres) : copie 1 sur disque local ou NAS (SMB/CIFS), copie 2 sur
  un deuxième NAS (réplique du dépôt, job `replicate`, mise à jour automatique et à la demande), état
  et erreurs d'application affichés ; service root `carborewind-storage.path` dans le paquet de l'agent.
  Migrations 0008.
- Sauvegarde manuelle : « Sauvegarde manuelle » au tableau de bord (tous les comptes ou un domaine,
  incrémentale ou complète) et « Exécuter maintenant » sur une politique.
- Profil des comptes (fonction, société, service, téléphones, adresse, description, notes, matricule) :
  colonnes, recherche, filtres et tri. Migration 0009.
- Menu de navigation à gauche ; les pages occupent toute la largeur.

### Fixed
- Quota et espace utilisé : quota hérité de la classe de service ou limite effective de Carbonio,
  espace mesuré boîte par boîte (`GetMailboxRequest`) quand `GetQuotaUsage` ne liste pas un compte,
  au lieu d'afficher 0. Diagnostic : `carborewind-agent carbonio quota <adresse>`.
- Migrations IMAP vers Carbonio : serveurs sources (TLS vérifié, mot de passe par boîte ou compte
  maître), lots avec options (dossier racine, exclusions, dates), import Excel/CSV ou saisie manuelle,
  association automatique (même adresse, alias, même nom avant le @ dans le domaine choisi), test de
  connexion, copie incrémentale reprenable sans doublon (UIDVALIDITY/UID, Message-ID), arrêt, oubli des
  mots de passe, notification de fin de lot. Écran **Migrations**, permission `migrations:manage`,
  migration 0007. Voir `docs/migrations.md`.
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
