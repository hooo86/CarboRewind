# Réglages de l'agent depuis l'interface

**Paramètres → Agent** (super-administrateurs). L'agent les récupère toutes les 30 secondes
environ et les applique **sans redémarrage** ; la page indique, pour chaque agent, si la dernière
version a été appliquée. Tant que rien n'a été enregistré ici, l'agent garde les valeurs de
`/etc/carborewind/agent.env`.

| Réglage | Défaut | Effet |
|---|---|---|
| Boîtes sauvegardées en même temps | 2 | nombre de jobs (sauvegarde, restauration, archivage, migration…) exécutés en parallèle, de 1 à 8 |
| Débit maximal | 0 (illimité) | plafond en Mbit/s des lectures dans Carbonio |
| Pause si la charge par processeur dépasse | 1,5 | avant chaque message, attend (5 min au plus) que la charge moyenne du serveur Carbonio redescende ; 0 = jamais |
| Disque local : garder libre | 20 Go / 15 % | une sauvegarde s'arrête avant de remplir le disque (la plus grande valeur s'applique) |
| NAS : garder libre | 2 Go | idem sur un partage réseau |
| Surveiller les services Carbonio | oui | état des services et des ports chaque minute (voir [monitoring.md](monitoring.md)) |
| Ports à tester | mta=25, amavis=10024, lmtp=7025, ldap=389 | un `nom=port` par ligne ; testés sur 127.0.0.1 puis sur le nom du serveur |
| Lire le journal du MTA | oui | trafic mail, spams, limites d'envoi (voir [mail-traffic.md](mail-traffic.md)) |

Restent dans `agent.env`, volontairement : l'adresse de l'API, les certificats, le chemin du
dépôt et de la copie 2, le fichier de clé, le compte d'administration Carbonio et les chemins des
journaux. Les modifier à distance pourrait couper l'agent du serveur ou affaiblir sa sécurité.

## Vitesse des sauvegardes

* Les jobs s'exécutent en parallèle selon le réglage ci-dessus (avant, un seul à la fois).
* Lors d'une sauvegarde complète, les métadonnées des messages (dossier, indicateurs, étiquettes,
  date) sont lues **par lots de 200** (`GetMsgMetadataRequest`) au lieu d'une requête par message ;
  si Carbonio refuse cette requête, l'agent revient à la lecture message par message.
* Sur le NAS, les entrées de répertoire sont synchronisées une fois par point de reprise (tous
  les 200 messages) et avant chaque manifeste, au lieu d'une fois par fichier ; un job interrompu
  reprend toujours depuis un point de reprise dont tous les fichiers sont durables.
* La première sauvegarde complète reste la plus longue ; les suivantes sont incrémentales.
