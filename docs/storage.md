# Stockage du dépôt de sauvegarde

Le dépôt (blobs chiffrés + manifestes) est écrit par l'agent, sur le serveur Carbonio. Il peut se trouver :

- sur un **disque local** (défaut : `/var/lib/carborewind/repository`) ;
- sur un **partage réseau SMB/CIFS** (NAS, serveur Windows) avec un nom d'utilisateur et un mot de passe ;
- sur un montage NFSv4 préparé par l'administrateur (voir `install.md`).

Les données sont **chiffrées par l'agent avant d'être écrites** (AES-256-GCM) : le NAS ne voit que des fichiers
opaques, et son administrateur ne peut pas lire les mails.

## Partage SMB/CIFS

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
