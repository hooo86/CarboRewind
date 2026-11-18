# CarboRewind

> Sauvegarde et restauration à chaud, point-in-time, pour serveurs de messagerie **Carbonio** (Zextras).
> Hot, point-in-time backup & restore for **Carbonio** (Zextras) mail servers.

[🇫🇷 Français](#-français) · [🇬🇧 English](#-english)

---

## 🇫🇷 Français

CarboRewind est une application web d'administration qui sauvegarde et restaure **sans arrêt du service** les données d'un serveur Carbonio : comptes, boîtes aux lettres (mails, dossiers, contacts, agendas, tâches, documents), configuration et métadonnées. Le nom évoque la restauration « point-in-time » : remonter le temps pour retrouver ses données.

> ⚠️ **Statut : en développement (pré-`v0.1.0`).** Ne pas utiliser en production.

### Fonctionnalités visées (MVP `v0.1.0`)

- Sauvegarde complète et incrémentale par compte, domaine ou globale, à chaud, avec limitation de charge
- Dépôt de sauvegarde chiffré (AES-256-GCM), compressé, dédupliqué, avec vérification d'intégrité
- Restauration d'un compte, d'un dossier ou d'un seul élément, à une date donnée, en mode écraser / fusionner / autre cible, avec dry-run
- Recherche et prévisualisation dans les sauvegardes
- Tableau de bord, journal d'audit inaltérable, notifications
- RBAC (super-admin, admin de domaine, opérateur restauration, lecteur seul), interface FR/EN

### Architecture

| Composant | Rôle | Technologie |
|---|---|---|
| `backend/` | API REST (OpenAPI), catalogue, audit, planification | Python · FastAPI · PostgreSQL · Celery/Redis |
| `agent/` | Worker sur le serveur Carbonio (SOAP/REST/LDAP), écrit le dépôt | Python · mTLS |
| `frontend/` | Interface d'administration | React · TypeScript · Vite |
| `deploy/` | Docker, docker-compose, scripts d'installation | |
| `docs/` | Architecture, guides, PRA, runbook | Markdown · Mermaid |

Voir [`docs/architecture.md`](docs/architecture.md).

### Compatibilité

- **Carbonio CE** : prise en charge prioritaire (API SOAP Admin, REST, LDAP).
- **Carbonio Advanced** : connecteur vers le module Backup natif prévu après le MVP.

### Démarrage rapide (développement)

```bash
cp deploy/.env.example deploy/.env   # puis renseigner les valeurs
docker compose -f deploy/docker-compose.dev.yml up --build
```

### Contribuer

Voir [`CONTRIBUTING.md`](CONTRIBUTING.md). Signaler une vulnérabilité : [`SECURITY.md`](SECURITY.md).

### Licence

[AGPL-3.0-only](LICENSE).

---

## 🇬🇧 English

CarboRewind is a web administration application that backs up and restores the data of a Carbonio mail server **with no service downtime**: accounts, mailboxes (mail, folders, contacts, calendars, tasks, files), configuration and metadata. The name refers to point-in-time recovery: rewind time to get your data back.

> ⚠️ **Status: under development (pre-`v0.1.0`).** Not for production use.

### Target features (MVP `v0.1.0`)

- Hot full and incremental backup per account, domain or globally, with throttling
- Encrypted (AES-256-GCM), compressed, deduplicated backup repository with integrity checks
- Restore a whole account, a folder or a single item, as of a given date, in overwrite / merge / alternate-target mode, with dry-run
- Search and preview inside backups
- Dashboard, tamper-evident audit log, notifications
- RBAC (super-admin, domain admin, restore operator, read-only), FR/EN UI

### Architecture

See the table above and [`docs/architecture.md`](docs/architecture.md).

### Compatibility

- **Carbonio CE**: first-class support (SOAP Admin API, REST, LDAP).
- **Carbonio Advanced**: connector for the native Backup module planned after the MVP.

### Quick start (development)

```bash
cp deploy/.env.example deploy/.env   # then fill in values
docker compose -f deploy/docker-compose.dev.yml up --build
```

### Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md). Report a vulnerability: [`SECURITY.md`](SECURITY.md).

### License

[AGPL-3.0-only](LICENSE).
