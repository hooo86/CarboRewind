# Restauration

La restauration relit **un manifeste complet** (n'importe quel snapshot : restauration *point-in-time*) dans le
dépôt chiffré de l'agent et réécrit les éléments choisis dans une boîte Carbonio, à chaud.

## Parcours dans l'interface

1. **Comptes** → un compte → **Historique des snapshots** → **Restaurer** sur le snapshot voulu.
2. **Que restaurer ?** tout le compte, des dossiers (avec leurs sous-dossiers) ou des éléments précis
   (recherche par objet ou expéditeur, filtre par dossier — droit `content:preview`).
3. **Où ?** même compte ou autre compte (du périmètre de l'utilisateur), et :
   - **nouveau dossier daté** (défaut, recommandé) : rien n'est modifié. Le courrier est recréé sous
     `/CarboRewind <date> UTC #<n>/…` en gardant l'arborescence ; contacts, agendas et tâches ont besoin de
     dossiers de leur type et vont sous `/<dossier> (CarboRewind <date> UTC #<n>)/…` ;
   - **dossiers d'origine** (même compte uniquement) : chaque élément retourne dans son dossier, recréé
     s'il a été supprimé ; un élément dont l'identifiant existe encore n'est **pas** dupliqué. C'est le mode
     « je retrouve ce qui a été effacé ».
4. **Simuler** calcule le plan sans rien écrire (éléments à restaurer, déjà présents, non pris en charge,
   dossiers à créer). **Restaurer** demande une confirmation.
5. Le résultat s'affiche dans la page et dans **Jobs**.

## Écriture dans Carbonio (CE)

| Type | Méthode |
|---|---|
| Courrier | `AddMsgRequest` avec drapeaux visibles (`ufrswd!?`), tags (relance sans tags s'ils n'existent plus) et date de réception ; contenu 8 bits via `/service/upload` |
| Contacts | REST `POST …/<dossier>?fmt=vcf&resolve=skip` |
| Rendez-vous, tâches | REST `POST …/<dossier>?fmt=ics&resolve=skip` (un UID déjà présent n'est pas dupliqué) |
| Fichiers (Files) | non pris en charge en CE, comptés à part |

## Sécurité et fiabilité

- `restore:run` sur le domaine **source et** le domaine **cible** ; refus audités ; mode « dossiers d'origine »
  interdit vers un autre compte ; snapshot expiré → 410 ; compte cible supprimé → 409.
- Le job est prioritaire sur les sauvegardes à la demande. Une erreur sur un élément n'arrête pas le job
  (comptée en échec) ; quota dépassé, compte inexistant ou droits refusés l'arrêtent.
- Points de reprise tous les 50 éléments : un job relancé ne réinjecte pas les messages déjà écrits.
- Audit : `restore.request`, `restore.dry_run`, `restore.complete` (source, snapshot, mode, compteurs).

## Limites actuelles

- Pas encore de restauration d'un **compte supprimé** dans un compte recréé (prochaine étape) ; il peut
  déjà servir de source vers un autre compte existant.
- Pas de mode « écraser » : on ne remplace jamais un élément existant.
