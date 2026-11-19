# Archivage

Les messages anciens sortent des boîtes Carbonio pour libérer du quota et du disque. Ils restent
conservés, chiffrés, dans le dépôt CarboRewind (copie 1, et copie 2 si elle est configurée), et
consultables depuis l'écran **Archives**.

## Politiques

**Archives → Politiques d'archivage** (super-administrateurs, permission `archives:manage`) :

| Réglage | Par défaut |
|---|---|
| Âge (date de réception) | plus de 365 jours (minimum 30) ; la date limite est minuit UTC |
| Comptes | domaines cochés (aucun = tous), seulement certains comptes, comptes exclus |
| Administrateurs | exclus, sauf case « Inclure les comptes administrateurs » |
| Dossiers | Corbeille, Spam, Brouillons (et leurs sous-dossiers) jamais ; plus les dossiers exclus de la politique ; les dossiers partagés par d'autres comptes ne sont pas parcourus |
| Messages épargnés | marqués d'un drapeau (et non lus, si coché) |
| Maximum | 5 000 messages par compte et par passage (le reste au passage suivant, du plus ancien au plus récent) |
| Planification | `30 1 * * *` (cron, fuseau de la politique) ; « Exécuter maintenant » à la demande |
| Conservation | illimitée, ou N jours après l'archivage |

Le compte technique de CarboRewind n'est jamais archivé.

### Modes

* **Simulation** : compte les messages concernés et leur volume, par dossier, sans rien copier.
* **Copie seule** : copie dans l'archive, laisse dans les boîtes. Les messages déjà copiés ne
  sont pas relus au passage suivant.
* **Archivage complet** : copie, vérifie, puis supprime des boîtes (suppression définitive, sans
  passer par la Corbeille, sinon le quota ne baisse pas).

**Une simulation est obligatoire** : le mode « Archivage complet » n'est accepté qu'après une
simulation terminée (sans échec) **après** la dernière modification des critères (comptes,
âge, dossiers, drapeaux, administrateurs). Si les critères changent ensuite, les passages
planifiés **simulent** en attendant une nouvelle simulation (l'écran l'indique).

## Ce que fait l'agent, compte par compte

1. **Sélection** : recherche Carbonio `before:<date>`, du plus ancien au plus récent. La date
   renvoyée par Carbonio est revérifiée : une requête mal interprétée ne peut que sélectionner
   moins de messages, jamais des messages plus récents.
2. **Stockage** : contenu brut et métadonnées (dossier, indicateurs, étiquettes, date de
   réception) dans le dépôt. Les blocs sont dédupliqués : un message déjà sauvegardé ne coûte
   presque rien. Chaque message est **relu, déchiffré et comparé octet par octet** à l'original.
3. **Index** : un manifeste d'archive chiffré est écrit dans le dépôt (`archives/<compte>/<id>.json`)
   et l'index (objet, expéditeur, destinataires, date, dossier, taille, pièces jointes) est envoyé
   au serveur **avant toute suppression**.
4. **Copie 2** : si elle est configurée, le dépôt est recopié et chaque message archivé est relu
   depuis la copie 2. En cas d'échec, rien n'est supprimé.
5. **Suppression** (archivage complet) : par lots de 100. Un message déjà absent compte comme
   retiré (il est dans l'archive).

Toute erreur avant l'étape 5 laisse la boîte intacte. L'avancement est enregistré : un job repris
ne copie ni ne supprime deux fois. Avant d'écrire, l'agent applique le garde-fou d'espace disque
du dépôt.

Chaque passage envoie un compte rendu par e-mail (événement `archive_report`) quand le dernier
compte est traité.

## Consulter, télécharger, restaurer

**Archives → Rechercher** (permission `content:preview`, limitée aux domaines de l'utilisateur) :

* recherche par compte, objet, expéditeur, destinataire, dossier, période, et « retirés des
  boîtes / encore dans les boîtes » ;
* **lecture** : l'agent déchiffre le message (job `fetch`, quelques secondes) ; le serveur le
  garde chiffré une heure. Le HTML est affiché **en texte seul** (aucune image, aucun lien externe
  chargé, aucun script) ; la liste des pièces jointes est affichée ;
* **téléchargement** du message complet en `.eml` (avec ses pièces jointes) ;
* **restauration** d'une sélection (`restore:run` sur le domaine source et cible) : dans un
  dossier « Archives restaurées <date> », dans le dossier d'origine, ou dans une autre boîte ;
  date de réception et indicateurs conservés. Les messages restent aussi dans l'archive.

Chaque lecture et chaque téléchargement sont audités (`archive.read`, `archive.download`).

## Conservation légale

**Archives → Conservation légale** (super-administrateurs) archive **toute une boîte** pour la
conserver, par exemple pour un dossier juridique ou une réquisition :

* tous les messages de tous les dossiers sont copiés, **y compris** Corbeille, Spam et Brouillons ;
  rien n'est retiré de la boîte ;
* tous les messages déjà archivés de ce compte passent aussi sous conservation ;
* la durée de conservation des politiques ne les supprime **jamais** tant que la conservation
  n'est pas levée (bouton « Lever la conservation ») ;
* le motif saisi est enregistré dans le journal d'audit (`archive.legal_hold`), comme la levée
  (`archive.legal_hold.release`) ;
* relancez l'archivage de la boîte pour prendre en compte les messages arrivés depuis (les
  messages déjà copiés ne sont pas relus). Une passe traite au plus 200 000 messages.

La recherche peut filtrer les messages « sous conservation légale ».

## Conservation et purge

Avec une conservation de N jours, les entrées d'index plus anciennes sont supprimées (jamais
pendant une restauration). La purge supprime ensuite du dépôt les blocs qu'aucune sauvegarde ni
archive n'utilise, et les manifestes d'archive devenus vides. Supprimer une politique ne supprime
pas ses archives : elles sont alors conservées sans limite.

## Points d'attention

* Un message archivé reste dans les **sauvegardes** existantes jusqu'à leur expiration ; une
  restauration de sauvegarde antérieure à l'archivage le ramène aussi (comportement normal).
* La recherche porte sur les en-têtes, **pas sur le texte** des messages (stocker le texte en
  clair dans la base serait un risque pour la confidentialité).
* Seuls les messages sont archivés (pas les contacts, agendas, tâches ni fichiers).
* Commencez par une simulation sur quelques comptes, puis une **copie seule**, avant
  l'archivage complet.
