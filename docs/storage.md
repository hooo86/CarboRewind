# Stockage du dépôt de sauvegarde

Le dépôt (blobs chiffrés + manifestes) est écrit par l'agent, sur le serveur Carbonio. Il peut se trouver :

- sur un **disque local** (défaut : `/var/lib/carborewind/repository`) ;
- sur un **partage réseau SMB/CIFS** (NAS, serveur Windows) avec un nom d'utilisateur et un mot de passe ;
- sur un montage NFSv4 préparé par l'administrateur (voir `install.md`).

Les données sont **chiffrées par l'agent avant d'être écrites** (AES-256-GCM) : le NAS ne voit que des fichiers
opaques, et son administrateur ne peut pas lire les mails.

## Depuis l'interface (recommandé)

**Paramètres → Stockage des sauvegardes** :

- **Copie 1 — dépôt de sauvegarde** : disque local du serveur Carbonio ou NAS (partage SMB/CIFS :
  `//serveur/partage/dossier`, utilisateur, mot de passe, domaine Windows facultatif, chiffrement
  SMB 3 facultatif). « Copier le dépôt actuel » (coché par défaut) recopie les sauvegardes existantes ;
  décoché, un dépôt vide est créé et les anciennes sauvegardes ne sont plus restaurables depuis
  l'interface.
- **Copie 2 — deuxième NAS** : une réplique du dépôt sur un autre partage, mise à jour par un job
  `replicate` après chaque nouvelle sauvegarde ou purge (au plus une fois par minute, une heure d'attente
  après un échec, alerte e-mail) et à la demande (« Mettre à jour la copie 2 maintenant »). Les fichiers
  supprimés par la rétention sont aussi supprimés de la copie. La copie est chiffrée comme l'original :
  pour restaurer depuis elle, il faut le fichier de clé `/etc/carborewind/repository.key` (à garder en
  séquestre). Une copie 2 absente ou démontée n'empêche jamais les sauvegardes ; rien n'est écrit tant
  que le partage n'est pas monté.

Fonctionnement : les mots de passe sont chiffrés (`CARBOREWIND_SECRET_KEY`) et ne sont jamais
réaffichés. L'agent (sans privilèges) récupère les paramètres en mTLS et les dépose dans
`/var/lib/carborewind/storage/request.json` ; l'unité `carborewind-storage.path` (installée et activée
par le paquet) déclenche `carborewind-agent storage apply` en root, qui :

1. lit la demande prudemment (pas de lien symbolique, propriétaire `carborewind`, taille bornée) et
   la supprime aussitôt ;
2. revalide tout ; seuls deux points de montage fixes sont possibles : `/var/lib/carborewind/nas`
   (copie 1) et `/var/lib/carborewind/copy02` (copie 2) ;
3. écrit les identifiants dans `/etc/carborewind/smb.credentials` / `smb-copy02.credentials` (root,
   0600), les unités de montage systemd, monte, vérifie l'écriture, copie ou initialise le dépôt,
   redémarre l'agent ;
4. écrit `status.json` (sans secret) : l'interface affiche « appliqué » ou l'erreur (ex.
   `mount error(13)` = identifiants refusés par le NAS).

Vérifier sur le serveur Carbonio :

```bash
systemctl status carborewind-storage.path
sudo journalctl -u carborewind-storage.service -n 50
```

## Partage SMB/CIFS en ligne de commande

Sur le serveur Carbonio (une seule fois, en root) :

```bash
sudo apt install cifs-utils
sudo carborewind-agent storage smb //nas.example.com/sauvegardes/carborewind --username svc-carborewind --domain EXEMPLE --migrate
```

Le mot de passe est demandé au clavier (ou `--password-file /chemin/fichier`), jamais sur la ligne de commande.
La commande :

1. enregistre les identifiants dans `/etc/carborewind/smb.credentials` (root, `0600`) ;
2. crée l'unité systemd `var-lib-carborewind-nas.mount` (SMB 3, fichiers appartenant à l'utilisateur
   `carborewind`, `nosuid,nodev,noexec`) et l'active au démarrage ;
3. impose à l'agent d'attendre ce montage (`RequiresMountsFor`) : si le NAS est absent, l'agent ne démarre pas
   au lieu de remplir le disque local ;
4. vérifie que l'agent peut créer, renommer et supprimer un fichier sur le partage ;
5. déplace le dépôt : `--migrate` copie le dépôt existant (les snapshots restent restaurables), `--start-empty`
   repart de zéro (les anciens snapshots ne seront plus restaurables). Sans l'une des deux options, rien n'est
   changé si un dépôt existe déjà ;
6. met à jour `CARBOREWIND_AGENT_REPOSITORY_PATH` dans `/etc/carborewind/agent.env`.

Ensuite : si le dépôt est nouveau, `sudo -u carborewind carborewind-agent repo init`, puis
`sudo systemctl restart carborewind-agent`.

Options : `--seal` (chiffrement de transport SMB 3, si le NAS le permet), `--mount-point` (sous
`/var/lib/carborewind/`).

Vérifier : `sudo carborewind-agent storage status` (emplacement, type de système de fichiers, espace libre). Le
tableau de bord affiche aussi l'emplacement et l'espace libre, avec une alerte sous 10 %.

Revenir au disque local : `sudo carborewind-agent storage local /var/lib/carborewind/repository`.

## Recommandations

- Un compte de service dédié sur le NAS, avec droits uniquement sur ce dossier.
- Une sauvegarde de la clé du dépôt (`/etc/carborewind/repository.key`) **hors du NAS** : sans elle, les
  données sont illisibles.
- Le partage doit gérer le renommage atomique (SMB 2/3 : oui) ; les verrous restent locaux au serveur
  Carbonio (un seul agent par dépôt).
