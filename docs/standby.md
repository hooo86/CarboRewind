# Serveur de secours (reprise rapide après sinistre)

Une seconde VM, **clone de la VM Carbonio** avec une autre adresse IP, est tenue à jour à
partir des sauvegardes CarboRewind. En cas de sinistre sur le serveur principal, on fait
pointer le DNS (ou la redirection du pare-feu) vers elle et on la **promeut** : les
utilisateurs retrouvent leurs boîtes telles qu'au dernier rejeu.

## Principe

- La VM de secours fait tourner Carbonio et un agent CarboRewind de **rôle « secours »**.
  Cet agent ne sauvegarde rien, son inventaire n'est pas fusionné aux comptes et son trafic
  mail est ignoré. Il ne prend que des jobs `standby_sync`.
- Le planificateur met en file, pour chaque boîte dont le dernier instantané n'est pas
  encore sur la VM de secours, un rejeu de cet instantané. Une boîte est rejouée **au plus
  toutes les N minutes** (réglable de 5 à 1440).
- L'agent de secours lit le **même dépôt** que le serveur principal (même partage SMB,
  même `repository.key`). Pour chaque boîte :
  - le compte est créé s'il manque (même zimbraId si Carbonio l'accepte, mot de passe
    aléatoire) ;
  - les messages sont ajoutés, supprimés, déplacés, et leurs drapeaux et tags mis à jour ;
  - un dossier de contacts, d'agenda ou de tâches est vidé puis réimporté quand son
    contenu a changé ;
  - les dossiers créés sont recréés, ceux qui ont été supprimés sont supprimés.
- Ce qui a été appliqué est noté par boîte dans `/var/lib/carborewind/standby/` sur la VM
  de secours : un rejeu ne coûte que les différences, et un rejeu interrompu reprend sans
  créer de doublons.
- **Premier rejeu après le clonage** : un clone de VM garde les identifiants des éléments.
  Un message du clone qui a le même identifiant qu'un message de l'instantané est donc
  considéré comme le même : il n'est pas recopié. Les messages du clone absents de
  l'instantané, supprimés sur le serveur principal depuis le clonage, sont supprimés.

**Retard réel** = fréquence des sauvegardes + intervalle de rejeu + durée du rejeu. Pour un
retard d'environ 30 minutes, programmez une sauvegarde incrémentale toutes les 15 minutes
(page Sauvegardes) et un intervalle de rejeu de 15 minutes.

## Ce qui n'est pas répliqué

- **Mots de passe des comptes créés après le clonage** : CarboRewind ne lit pas les mots
  de passe. Les comptes présents au moment du clonage gardent ceux du clone.
- **Réglages modifiés après le clonage** sur le serveur principal : listes de diffusion,
  alias, partages, filtres, signatures, classes de service, réglages globaux. Refaites un
  clone après un gros changement de configuration.
- **Fichiers (Files / Briefcase)**, comme pour la restauration.
- Les messages arrivés depuis la dernière sauvegarde (voir le retard ci-dessus).

## Mise en place (VMware ESXi)

1. **Le dépôt doit être sur un partage réseau** (Paramètres → Stockage : partage SMB) :
   un dépôt « local » est sur le disque de la VM Carbonio, et le clone n'en aurait qu'une
   copie figée. Vérifiez sur le serveur Carbonio :

   ```bash
   sudo findmnt -T /var/lib/carborewind/repository
   ```

   La colonne FSTYPE doit afficher `cifs`.

2. **Clonez la VM** juste après une sauvegarde complète : clone de la VM éteinte, ou
   export OVF avec `ovftool` sur un ESXi sans vCenter. Dans les réglages de la carte
   réseau du clone, **décochez « Connecter à la mise sous tension »**. Le clone ne doit pas
   apparaître sur le réseau avec l'IP et le certificat d'agent du serveur principal.

3. **Démarrez le clone sans réseau**, puis, dans la console ESXi :

   Arrêtez l'agent copié, qui porte l'identité de l'agent principal :

   ```bash
   sudo systemctl disable --now carborewind-agent
   ```

   Oubliez son certificat :

   ```bash
   sudo rm -f /var/lib/carborewind/tls/agent.crt /var/lib/carborewind/tls/agent.key
   ```

   Videz la file Postfix copiée (sinon ces messages partiraient une seconde fois) :

   ```bash
   sudo su - zextras -c "postsuper -d ALL"
   ```

   Changez l'adresse IP dans netplan, puis dans `/etc/hosts`. Le nom `mail.lnhc.dz` reste
   le même : Carbonio y tient.

   ```bash
   sudo nano /etc/netplan/*.yaml
   ```

   ```bash
   sudo nano /etc/hosts
   ```

   ```bash
   sudo netplan apply
   ```

4. **Reconnectez la carte réseau**, puis vérifiez que Carbonio démarre avec la nouvelle IP :

   ```bash
   sudo su - zextras -c "zmcontrol status"
   ```

   Si `service-discover` ne démarre pas à cause de l'ancienne IP, relancez son assistant
   de configuration (`service-discover setup-wizard`). Ce point n'a pas été vérifié sur
   Carbonio CE : notez ce que vous avez dû faire.

5. **Enrôlez l'agent de secours.** Sur la VM CarboRewind :

   ```bash
   docker compose -f docker-compose.prod.yml run --rm api carborewind-api agent-create secours mail.lnhc.dz --role standby
   ```

   Puis sur le clone, en collant le jeton affiché :

   ```bash
   sudo carborewind-agent enroll
   ```

   ```bash
   sudo systemctl enable --now carborewind-agent
   ```

6. Dans l'interface, page **Serveur de secours** : cochez « Répliquer vers la VM de
   secours », réglez le retard souhaité et enregistrez. Le premier rejeu de chaque boîte
   est plus long que les suivants. Le retard et les boîtes en échec s'affichent sur cette
   page.

## Bascule (sinistre)

1. Vérifiez le retard sur la page **Serveur de secours**.
2. Faites pointer les enregistrements DNS A et MX, ou la redirection NAT du pare-feu, vers
   l'IP de la VM de secours.
3. Cliquez sur **Basculer sur …** et tapez le nom de l'agent pour confirmer. L'agent de
   secours devient principal, la réplication s'arrête, les rejeux en file sont annulés et
   les sauvegardes reprennent depuis la VM de secours. Comme les comptes ont les mêmes
   identifiants, l'historique des sauvegardes continue.
4. Redéfinissez le mot de passe des comptes créés après le clonage.

L'ancien serveur principal passe en rôle secours : s'il redémarre, il n'est plus
sauvegardé à la place du nouveau.

## Retour sur le serveur d'origine

Une fois l'ancien serveur réparé, la méthode la plus sûre est d'en faire le nouveau
secours. Son agent a déjà le rôle secours depuis la bascule. Réactivez la réplication : il
rattrape les boîtes depuis les sauvegardes du nouveau principal. Basculez ensuite dans
l'autre sens à une heure creuse.
