# Journal d'audit / Audit trail

> Issues #3 à #5. Exigence : « journal d'audit complet et inaltérable (qui a restauré quoi, quand) ».

## Garanties

| Menace | Mesure |
|---|---|
| Modification ou suppression par l'application | rôle `carborewind_app` sans `UPDATE`, `DELETE` ni `TRUNCATE` sur `audit_events` ([`deploy/sql/grants.sql`](../deploy/sql/grants.sql)) |
| Modification par n'importe quel rôle (bug, injection SQL) | trigger `audit_events_append_only` qui refuse `UPDATE`, `DELETE` et `TRUNCATE` |
| Modification par le propriétaire de la table (qui peut désactiver le trigger) | **chaîne de hash** : `hash = SHA-256(prev_hash ‖ JSON canonique)`. Toute altération d'un événement casse la chaîne à partir de lui. `GET /api/audit/verify` ou `carborewind-api audit-verify` la recalcule |
| Réécriture complète de la chaîne par le propriétaire | **ancrage externe** : exporter régulièrement `GET /api/audit/head` (id et hash de tête) hors de la base, par exemple dans un ticket, un e-mail, un SIEM ou un stockage WORM. Une tête ancrée qui ne figure plus dans la chaîne révèle une réécriture |
| Écritures concurrentes | verrou consultatif transactionnel : la chaîne reste linéaire |

L'export CSV neutralise l'**injection de formules** : une cellule commençant par `= + - @` reçoit
le préfixe `'`.

## Événements

| Action | Émis par | Détails |
|---|---|---|
| `auth.login` | connexion | `reason` en cas d'échec : `bad_credentials`, `locked`, `inactive`, `no_role`, `rate_limited` |
| `auth.logout` | déconnexion | — |
| `authz.denied` | refus RBAC | `permission`, `method`, `path` (et `domain` le cas échéant) |
| `user.create`, `user.update`, `user.role.grant`, `user.role.revoke` | administration | changements, sessions révoquées |
| `audit.export`, `audit.verify` | consultation de l'audit | filtres utilisés, résultat |
| `backup.*`, `restore.*` | à venir (M2, M3) | compte, snapshot, éléments, mode, cible |

## Consultation

| Route | Qui | Contenu |
|---|---|---|
| `GET /api/audit` | `audit:read` | paginé, du plus récent au plus ancien. Filtres : `action` (exacte ou préfixe `restore.*`), `actor`, `outcome`, `target_id`, `since`, `until` |
| `GET /api/audit/export.csv` | `audit:read` | mêmes filtres, du plus ancien au plus récent, en flux. L'export est lui-même audité |
| `GET /api/audit/verify` | `audit:read` **global** | recalcul complet de la chaîne |
| `GET /api/audit/head` | `audit:read` **global** | tête de chaîne, à ancrer hors de la base |

Un `domain_admin` ne voit que les événements de ses domaines, plus ses propres actions.

## Exploitation

```bash
# vérification complète (code de sortie 1 si la chaîne est rompue) — à planifier (cron)
docker compose run --rm api carborewind-api audit-verify
```

En cas de chaîne rompue, suivez le runbook (M5) : figer la base, comparer avec la dernière
tête ancrée, et identifier l'événement `broken_id` ainsi que les accès propriétaire récents.
