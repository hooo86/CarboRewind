# Modèle de données / Data model

Base PostgreSQL 16 du plan de contrôle. Source de vérité : les modèles SQLAlchemy
(`backend/src/carborewind_api/db/models.py`) et les migrations Alembic (`backend/migrations/`).
Le SQL complet est généré dans [`schema.sql`](schema.sql). Un test vérifie qu'il est à jour.

> Les **données** des boîtes aux lettres ne sont pas en base : elles sont dans le dépôt de
> sauvegarde (blobs chiffrés). La base ne contient que le catalogue (métadonnées de recherche),
> les jobs, la configuration et l'audit.

## Diagramme entité-relation

```mermaid
erDiagram
  users ||--o{ role_assignments : "a"
  domains ||--o{ role_assignments : "périmètre"
  domains ||--o{ accounts : "contient"
  accounts ||--o{ snapshots : "a"
  accounts ||--o{ item_versions : "a"
  accounts ||--o{ folder_versions : "a"
  accounts ||--o{ jobs : "cible"
  policies ||--o{ jobs : "déclenche"
  agents ||--o{ jobs : "exécute"
  users ||--o{ jobs : "crée"
  jobs ||--o| snapshots : "produit"
  blobs ||--o{ item_versions : "contenu / méta"

  users {
    uuid id PK
    string username UK
    enum auth_source "local|ldap|oidc"
    string password_hash "Argon2id, local uniquement"
    int failed_logins
    timestamptz locked_until
  }
  role_assignments {
    uuid id PK
    uuid user_id FK
    enum role "super_admin|domain_admin|restore_operator|viewer"
    uuid domain_id FK "NULL = global"
  }
  domains {
    uuid id PK
    string name UK
  }
  accounts {
    uuid id PK
    uuid domain_id FK
    string email "unique si non supprimé"
    string carbonio_id UK "zimbraId"
    enum status "active|locked|closed|deleted"
  }
  agents {
    uuid id PK
    string name UK
    enum status "pending|active|revoked"
    string cert_fingerprint UK "SHA-256 du certificat mTLS"
  }
  policies {
    uuid id PK
    enum target_type "account|domain|global"
    uuid target_id
    enum backup_type "full|incremental"
    string schedule "cron"
    time window_start
    time window_end
    jsonb retention
  }
  jobs {
    uuid id PK
    enum kind "backup|restore|config_backup|verify|purge"
    enum status "queued|running|succeeded|failed|cancelled"
    jsonb params
    jsonb checkpoint "point de reprise"
    timestamptz lease_expires_at "bail de l'agent"
    string idempotency_key UK
    int attempts
  }
  snapshots {
    uuid id PK
    uuid account_id FK
    int seq "monotone par compte"
    string manifest_ref
    string sync_token "SyncRequest"
    jsonb account_attrs "pour recréer un compte supprimé"
  }
  blobs {
    string id PK "HMAC-SHA-256 hex"
    bigint size
    bigint stored_size
  }
  item_versions {
    bigint id PK
    uuid account_id FK
    int carbonio_item_id
    enum kind
    string content_blob_id FK
    text subject "index trigramme"
    text sender "index trigramme"
    timestamptz item_date
    int from_seq
    int to_seq "NULL = toujours présent"
  }
  folder_versions {
    bigint id PK
    uuid account_id FK
    int carbonio_folder_id
    text path
    int from_seq
    int to_seq
  }
  audit_events {
    bigint id PK
    timestamptz ts
    enum actor_type
    string action
    enum outcome
    inet source_ip
    string prev_hash
    string hash UK
  }
```

## Choix de conception

### Catalogue temporel (point-in-time sans duplication)

Chaque snapshot d'un compte reçoit un numéro `seq` croissant. Une ligne de `item_versions` est
valable pour les snapshots `from_seq <= seq < to_seq`, et `to_seq` vaut NULL tant que l'élément existe.

| Évènement détecté (via `SyncRequest`) | Effet sur le catalogue au snapshot *N* |
|---|---|
| Création | nouvelle ligne `from_seq = N` |
| Modification | `to_seq = N` sur la ligne ouverte, puis nouvelle ligne `from_seq = N` |
| Suppression | `to_seq = N` sur la ligne ouverte |

Lister le contenu d'une boîte à l'instant T revient à :

```sql
SELECT … FROM item_versions
WHERE account_id = :a AND from_seq <= :n AND (to_seq IS NULL OR to_seq > :n);
```

La taille du catalogue croît avec le nombre de **changements** et non avec
`éléments × snapshots`. Pour 5 000 comptes à environ 20 000 éléments, cela représente de l'ordre
de 10⁸ lignes, ce que PostgreSQL gère avec les index fournis. Si la volumétrie l'exige, la table
pourra être partitionnée par `HASH(account_id)` (migration ultérieure).

Un index unique partiel (`to_seq IS NULL`) garantit **une seule version ouverte** par élément.

### Jobs : idempotence et reprise

- `idempotency_key` (par exemple `policy:<id>:<horodatage planifié>`) empêche les doublons de planification.
- Un job `running` a toujours un agent et un bail `lease_expires_at`, garantis par une contrainte CHECK.
  Un bail expiré signale un job abandonné, que l'agent reprend depuis `checkpoint`.
- `attempts <= max_attempts` est garanti par une contrainte CHECK.

### Audit inaltérable

- `audit_events` est en *append-only*. Un trigger refuse `UPDATE`, `DELETE` et `TRUNCATE`, même
  pour le propriétaire de la table. En production, le rôle applicatif ne reçoit que `INSERT` et `SELECT`.
- Chaque entrée porte `prev_hash` et `hash = SHA-256(prev_hash ‖ JSON canonique)`. Le calcul et
  la vérification de la chaîne arrivent avec #5.

### Autres contraintes notables

| Contrainte | Rôle |
|---|---|
| `users.local_has_password` | un compte local a toujours un hash |
| `role_assignments.super_admin_is_global` | super-admin jamais limité à un domaine |
| `uq_role_assignments_grant` (NULLS NOT DISTINCT) | pas de double attribution, y compris globale |
| `uq_accounts_email_live` (partiel) | une adresse est réutilisable après suppression du compte |
| `policies.target_id_matches_type` | cible cohérente avec son type |
| `blobs.id_is_hex_sha256` | identifiant de blob bien formé |
| Énumérations en `VARCHAR` + `CHECK` | ajout d'une valeur par simple migration |

## Commandes

```bash
cd backend
export CARBOREWIND_DATABASE_URL=postgresql+psycopg://user:pass@host/db
alembic upgrade head              # appliquer les migrations
alembic revision --autogenerate -m "…"   # après modification des modèles
python tools/gen_schema.py        # régénérer docs/schema.sql
```
