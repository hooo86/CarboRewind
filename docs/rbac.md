# Rôles et permissions (RBAC)

> Source de vérité : `backend/src/carborewind_api/authz.py`. La matrice ci-dessous est
> **générée** par `python backend/tools/gen_docs.py`, et un test vérifie qu'elle est à jour.

## Rôles

| Rôle | Portée | Usage typique |
|---|---|---|
| `super_admin` | toujours globale | administration complète de CarboRewind : utilisateurs, agents, paramètres |
| `domain_admin` | globale ou un domaine | responsable d'un ou plusieurs domaines : sauvegardes, restaurations, politiques, audit |
| `restore_operator` | globale ou un domaine | support : recherche, prévisualisation et restauration, sans modifier les politiques |
| `viewer` | globale ou un domaine | supervision : état des sauvegardes et métadonnées, **sans accès au contenu des mails** |

Un utilisateur peut cumuler plusieurs attributions, par exemple `viewer` global et
`restore_operator` sur `example.org`. Ses droits effectifs sont l'**union** des attributions,
chacune avec sa portée.

## Matrice

<!-- BEGIN GENERATED MATRIX -->
| Permission | `super_admin` | `domain_admin` | `restore_operator` | `viewer` |
|---|---|---|---|---|
| `inventory:read` | ✅ | ✅ | ✅ | ✅ |
| `backups:read` | ✅ | ✅ | ✅ | ✅ |
| `content:preview` | ✅ | ✅ | ✅ | — |
| `backups:run` | ✅ | ✅ | — | — |
| `restore:run` | ✅ | ✅ | ✅ | — |
| `policies:manage` | ✅ | ✅ | — | — |
| `audit:read` | ✅ | ✅ | — | — |
| `users:manage` (global uniquement) | ✅ | — | — | — |
| `agents:manage` (global uniquement) | ✅ | — | — | — |
| `settings:manage` (global uniquement) | ✅ | — | — | — |
<!-- END GENERATED MATRIX -->

Les permissions marquées *global uniquement* ne sont jamais accordées par une attribution
limitée à un domaine, même en cas d'erreur de configuration (défense en profondeur, en plus de
la contrainte SQL `super_admin_is_global`).

## Application côté API

1. **Authentification** : sans session valide, la réponse est 401.
2. **Permission sur la route** : `require(permission)` vérifie que l'utilisateur détient la
   permission globalement ou sur au moins un domaine. Sinon, la réponse est 403 et l'événement
   `authz.denied` est **audité**.
3. **Portée sur l'objet** : la route vérifie le domaine de l'objet visé
   (`Access.allows(perm, domain_id)`) ou filtre sa requête (`Access.domains(perm)`).
   Un utilisateur limité à un domaine reçoit 403 pour un domaine **inconnu** comme pour un domaine
   **interdit**, ce qui évite de révéler l'existence d'un domaine.

## Garde-fous de l'administration des utilisateurs

| Règle | Comportement |
|---|---|
| Dernier `super_admin` actif | impossible de le désactiver ou de lui retirer le rôle (409) |
| Auto-désactivation | refusée (409) |
| `super_admin` limité à un domaine | refusé (422 côté API, contrainte CHECK côté base) |
| Retrait d'un rôle, désactivation | **révocation immédiate de toutes les sessions** de l'utilisateur |
| Compte LDAP | pré-enregistrable sans mot de passe ; un rôle peut lui être attribué avant sa première connexion |
| Toute modification | auditée : `user.create`, `user.update`, `user.role.grant`, `user.role.revoke` |

## Routes

| Méthode | Chemin | Permission |
|---|---|---|
| GET / POST | `/api/users` | `users:manage` |
| GET / PATCH | `/api/users/{id}` | `users:manage` |
| POST | `/api/users/{id}/roles` | `users:manage` |
| DELETE | `/api/users/{id}/roles/{assignment_id}` | `users:manage` |
| GET | `/api/domains` | `inventory:read` (filtré par portée) |
| GET | `/api/domains/{id}/accounts` | `inventory:read` sur ce domaine |

La spécification complète se trouve dans [`openapi.json`](openapi.json), également servie sur `/api/docs`.
