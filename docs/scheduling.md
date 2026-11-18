# Planification, rétention et purge

## Politiques

Une politique (**Politiques** dans l'interface, `POST /api/policies`) définit :

| Champ | Rôle |
|---|---|
| Portée | tous les comptes, un domaine ou un compte |
| Planification | expression cron à 5 champs (`minute heure jour mois jour-de-semaine`), évaluée dans le **fuseau** choisi (ex. `Africa/Algiers`). L'interface propose « chaque jour à… », « chaque semaine le… à… » ou une expression libre |
| Type | incrémentale (défaut) ou complète |
| Rétention | voir ci-dessous |
| Priorité, activée | ordre des jobs ; une politique suspendue ne lance rien |

Droits : `policies:manage` (super-admin pour les politiques globales, admin de domaine pour son domaine et ses comptes).
Création, modification et suppression sont auditées.

## Ordonnanceur

Le service `scheduler` (`carborewind-api scheduler`, conteneur dédié) fait un passage par minute :

1. pour chaque politique active dont un créneau est échu depuis le dernier passage, il crée un job de sauvegarde
   par compte actif ou verrouillé couvert ; un compte qui a déjà une sauvegarde en attente ou en cours est ignoré ;
2. une fois par heure : rétention puis demande de purge.

Si l'ordonnanceur a été arrêté, seul le **dernier** créneau manqué (dans la limite de 7 jours) est rattrapé.
La clé d'idempotence `policy:<id>:<créneau>:<compte>` et un verrou PostgreSQL empêchent tout doublon, même avec
plusieurs ordonnanceurs. Test manuel : `docker compose -f docker-compose.prod.yml run --rm api carborewind-api scheduler --once`.

## Rétention (grand-père / père / fils)

Pour chaque compte couvert : les **N derniers** snapshots, plus le plus récent de chacun des **N derniers jours**,
**semaines** (ISO) et **mois**. Toutes les valeurs à 0 = tout conserver. Si plusieurs politiques couvrent un compte,
la plus généreuse s'applique (et « tout conserver » l'emporte). Ne sont jamais expirés : le snapshot le plus
récent (base de l'incrémentale suivante) et un snapshot qu'une restauration en attente va lire.

Expirer un snapshot le marque `expired` (il n'est plus restaurable) et supprime du catalogue les versions
d'éléments et de dossiers dont aucun snapshot restant n'a besoin.

## Purge du dépôt

L'ordonnanceur crée un job `purge` (un seul à la fois) s'il reste des manifestes expirés ou des blobs orphelins.
La liste de ce qui peut être supprimé est calculée **au moment où l'agent prend le job** : l'agent n'exécute
alors rien d'autre, donc aucune sauvegarde ne s'appuie sur ces blobs (déduplication). Les blobs cités dans le
point de reprise d'une sauvegarde inachevée sont exclus. L'agent supprime manifestes et blobs sous le verrou
exclusif du dépôt, puis l'API les oublie. Le job indique le volume libéré.
