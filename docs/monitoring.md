# Supervision

Menu **Supervision** (super-administrateurs) et métriques Prometheus.

## Ce qui est suivi

| Sujet | Détail | Alerte e-mail |
|---|---|---|
| **Stockage** (copie 1 et copie 2) | espace utilisé sur 30 jours (une mesure par heure, envoyée par l'agent), croissance par jour, **prévision de saturation** (droite des moindres carrés sur l'espace utilisé) | « Stockage plein dans moins de 14 jours », une fois par jour |
| **Comptes à risque** | jamais sauvegardés ; sans sauvegarde réussie depuis N heures (24 par défaut) ; dernière sauvegarde en échec ; dernière sauvegarde partielle ; couverts par aucune politique active. Chaque chiffre ouvre la liste des comptes filtrée | (résumé quotidien existant) |
| **Retard de la copie 2** | dernière mise à jour réussie, retard quand des sauvegardes ou purges ont eu lieu depuis | « Copie 2 en retard » au-delà de N heures (24 par défaut), une fois par jour |
| **Intégrité** | job `verify` planifié (par défaut le dimanche à 3 h 30) qui déchiffre et authentifie un échantillon des fichiers (10 % par défaut, un échantillon différent à chaque fois) de la copie 1 puis de la copie 2 ; vérification complète à la demande | « Corruption détectée » |
| **Boîtes presque pleines** | boîtes dont l'espace utilisé atteint le seuil (90 % du quota par défaut) ou dépasse une taille fixe en Go (0 = désactivé) ; liste dans « Comptes à risque » et nombre sur le tableau de bord | « Boîtes mail proches du quota », résumé une fois par jour |
| **Stockage des e-mails** (tableau de bord) | volume total des boîtes, quotas attribués, occupation du disque du serveur Carbonio (`/opt/zextras`, mesuré par l'agent) et du NAS de sauvegarde (copie 1 et 2), part des e-mails sur chacun | — |
| **Agents** | version, dernier contact, expiration du certificat, dernières statistiques mail | « Agent silencieux » (existant) |

Réglages : **Supervision → Réglages de la supervision** (seuils, seuil des boîtes presque pleines, planification cron, part des fichiers
vérifiés, fuseau). Une vérification complète relit tout le dépôt : à réserver à une plage creuse.

## Services Carbonio et file d'attente

Un fil de l'agent relève chaque minute, sur le serveur Carbonio :

* l'état de chaque service tel que `zmcontrol status` l'affiche (amavis, antispam, antivirus,
  mta, mailbox, opendkim, cbpolicyd…), par l'API d'administration (`GetServiceStatusRequest`,
  aucun accès shell). Un relevé de plus de 15 minutes est ignoré (état « inconnu ») : le service
  `stats` qui l'enregistre peut lui-même être arrêté ;
* les ports locaux essentiels : MTA 25, Amavis 10024, LMTP 7025, LDAP 389 (réglable :
  `CARBOREWIND_AGENT_SERVICE_PORTS='{"mta":25,"amavis":10024}'`, `CARBOREWIND_AGENT_SERVICE_MONITOR=false`
  pour désactiver). Ces sondes fonctionnent même si l'API d'administration ne répond plus ;
* la taille des files d'attente du MTA (deferred, active, incoming, hold).

Alertes (Paramètres → Notifications) :

| Événement | Quand |
|---|---|
| `service_down` | un service est arrêté sur **2 relevés consécutifs** (réglable) : un redémarrage de quelques secondes n'alerte pas ; une seule alerte par panne |
| `service_up` | le service fonctionne à nouveau |
| `mail_queue` | la file d'attente dépasse le seuil (200 messages par défaut, 0 = désactivé), au plus toutes les 3 heures |

La page **Supervision** affiche les services (arrêtés en premier) et la file d'attente ; le tableau
de bord affiche un bandeau rouge tant qu'un service est arrêté. Métriques Prometheus :
`carborewind_carbonio_service_up{agent,service}` et `carborewind_mail_queue_messages{agent}`.

Quand Amavis est arrêté, Postfix ne peut plus lui confier les messages : **envoi et réception
s'arrêtent**, les messages attendent dans la file (rien n'est perdu). Correction :

```bash
sudo su - zextras -c "zmcontrol start"
sudo su - zextras -c "postqueue -f"
```

## Redémarrage automatique de Carbonio et test d'envoi

**Paramètres → Agent → Redémarrage automatique de Carbonio et test d'envoi** (désactivé par
défaut) :

* quand le MTA (25), le LMTP (7025), l'annuaire (389) ou le serveur de boîtes restent arrêtés
  pendant **N minutes** (5 par défaut, de 1 à 240), l'agent demande `zmcontrol start` ;
* l'agent n'a pas les droits root : il écrit `/var/lib/carborewind/control/request.json`, et
  l'unité `carborewind-control.path` lance `carborewind-agent control apply` en root. Cette
  commande revalide la demande (seule l'action « start » est acceptée), puis exécute la commande
  fixe `su - zextras -c "zmcontrol start"`. Le résultat (code, fin de la sortie) revient dans
  `status.json` ;
* **jamais pendant une installation de paquets** (apt / dpkg en cours : une mise à jour arrête
  Carbonio volontairement), jamais quand il est **suspendu** depuis la Supervision
  (« Suspendre 2 h » avant une intervention planifiée), au plus une fois toutes les 15 minutes
  et 3 fois en 6 heures. Au-delà, une intervention est nécessaire ;
* dès que les services répondent, un **message de test** part vers l'adresse interne et
  l'adresse externe choisies. Il est remis au Postfix local (127.0.0.1:25, expéditeur : le
  compte Carbonio de l'agent). Son sort est lu dans le journal du MTA grâce à son
  Message-ID : **distribué** (relais LMTP 7025 pour l'interne, serveur distant pour
  l'externe), **différé** ou **refusé**, avec la raison. Sans nouvelles au bout de 10 minutes,
  le test est en échec ;
* **Tester l'envoi maintenant** (Supervision) lance le même test à la demande ;
* alertes : `carbonio_autostart` (démarrage lancé), `carbonio_autostart_failed`,
  `mail_test_failed`, et `mail_test_ok` quand l'envoi fonctionne après un redémarrage.

## Listes noires (DNSBL)

**Paramètres → Listes noires** : les adresses IP publiques du serveur de messagerie sont
vérifiées toutes les N minutes (60 par défaut, 15 au minimum) et à la demande, dans les listes
choisies (par défaut zen.spamhaus.org, bl.spamcop.net, b.barracudacentral.org,
psbl.surriel.com, bl.mailspike.net, dnsbl-1.uceprotect.net).
* Les listes fermées ne sont plus interrogées et sont retirées des réglages déjà enregistrés
  (remplacées par bl.mailspike.net) : ix.dnsbl.manitu.net (NiX Spam, fermée le 16 janvier
  2025), dnsbl.sorbs.net (SORBS, juin 2024), dnsbl.njabl.org. Une liste fermée peut se mettre à
  répondre « listée » pour toutes les adresses ; l'enregistrer est refusé.

* Requête DNS standard : adresse inversée + zone de la liste (IPv4 et IPv6). Réponse en
  `127.0.0.0/8` = listée ; nom inexistant = non listée.
* **Spamhaus refuse les résolveurs DNS publics** (8.8.8.8, 1.1.1.1…) : il répond
  `127.255.255.25x`, affiché comme « inconnu », jamais comme une inscription. Le serveur
  CarboRewind doit utiliser le DNS local ou celui du fournisseur d'accès.
* Une inscription envoie l'alerte `blacklisted` (une fois), un retrait `blacklist_cleared`. Une
  vérification en échec (DNS injoignable) ne change rien.
* La Supervision affiche le tableau adresse × liste ; le tableau de bord affiche un bandeau rouge
  tant qu'une adresse est listée ; Prometheus : `carborewind_blacklisted{ip,zone}`.

## Garde-fous

- **Espace disque** : l'agent refuse d'écrire une sauvegarde quand il ne resterait pas assez de place
  sur le système de fichiers du dépôt. Sur un disque local (partagé avec Carbonio) : max(20 Gio, 15 %
  du disque) doivent rester libres ; sur un partage réseau : 2 Gio. Le job échoue (sans nouvel essai)
  avec une alerte. Réglable dans `/etc/carborewind/agent.env` :
  `CARBOREWIND_AGENT_MIN_FREE_GB_LOCAL`, `CARBOREWIND_AGENT_MIN_FREE_PERCENT_LOCAL`,
  `CARBOREWIND_AGENT_MIN_FREE_GB_NETWORK`. La copie 2 s'arrête aussi avant de remplir son partage.
- **Verrouillages automatiques** (trafic mail) : jamais un compte administrateur, jamais le compte
  technique de CarboRewind, au plus 3 par heure (réglable) ; au-delà, alerte seulement.
- **Mesure de la taille des boîtes** : au plus 200 appels `GetMailboxRequest` par inventaire, à tour
  de rôle.

## Prometheus et Grafana

`https://<serveur CarboRewind>/metrics` exige l'en-tête `Authorization: Bearer <jeton>` ; le jeton est
`CARBOREWIND_METRICS_TOKEN` dans `/opt/carborewind/deploy/.env` (généré par `install-server.sh`).
Sans jeton configuré, `/metrics` n'est pas exposé à l'extérieur.

Fichiers prêts à l'emploi dans `deploy/monitoring/` :

- `prometheus-scrape.yml` : configuration de collecte ;
- `carborewind-alerts.yml` : règles d'alerte (comptes non protégés, jobs en échec, stockage plein
  bientôt ou sous 10 %, copie 2 en retard, corruption, intégrité non vérifiée depuis 8 jours, agent
  silencieux, certificat bientôt expiré, spam émis, alertes d'envoi ouvertes) ;
- `grafana-dashboard.json` : tableau de bord (Grafana → Dashboards → Import, choisir la source
  Prometheus).

Métriques principales (jauges, sans libellé par compte) :

| Métrique | Libellés |
|---|---|
| `carborewind_accounts` | `status` |
| `carborewind_accounts_at_risk` | `reason` = never, stale, failed, partial, no_policy |
| `carborewind_oldest_backup_age_seconds` | |
| `carborewind_jobs`, `carborewind_jobs_failed_24h` | `kind`, `status` |
| `carborewind_storage_total_bytes`, `_free_bytes`, `_days_until_full` | `agent`, `target` = primary, replica |
| `carborewind_replica_lag_seconds` | |
| `carborewind_integrity_last_check_timestamp_seconds`, `carborewind_integrity_corrupted_files` | `target` |
| `carborewind_agent_last_seen_seconds`, `carborewind_agent_certificate_expiry_seconds` | `agent` |
| `carborewind_mail_sent_current_hour`, `_spam_received_current_hour`, `_spam_sent_current_hour`, `carborewind_mail_alerts_open` | |

## Certificat TLS (date d'expiration, renouvellement Let's Encrypt)

**Contrôle** : toutes les heures, l'agent lit le certificat présenté sur HTTPS 443,
IMAPS 993, SMTP 25 et submission 587 (STARTTLS). Il se connecte en local, avec le nom
du serveur choisi dans Paramètres → Agent : « Nom du serveur », ou le nom de la machine
si le champ est vide. La Supervision affiche, port par port, les noms du certificat,
l'émetteur et la date d'expiration. Elle signale un certificat auto-signé ou qui ne
correspond pas au nom.

**Alerte** : sous le seuil (7 jours par défaut), une alerte `certificate_expiring` part
toutes les heures par e-mail et Telegram, jusqu'au renouvellement.
`certificate_expired` part quand le certificat est expiré.

**Renouvellement** : automatique (désactivé par défaut, déclenché à N jours de
l'expiration, 7 par défaut) ou avec le bouton « Renouveler maintenant » de la
Supervision. L'agent n'a pas les droits root : il écrit
`/var/lib/carborewind/cert/request.json`, et l'unité `carborewind-cert.path` lance en root
`carborewind-agent cert apply`, qui :

1. vérifie la demande (action `renew`, nom d'hôte valide ; aucun shell ne reçoit le nom) ;
2. lance `certbot renew --cert-name <nom> --non-interactive` : le certbot de Carbonio
   (`/opt/zextras/libexec/certbot`, en zextras) s'il existe, sinon celui du système ;
3. si certbot a produit un certificat plus récent que celui de Carbonio, copie la clé
   dans `/opt/zextras/ssl/carbonio/commercial/commercial.key`, puis lance
   `zmcertmgr deploycrt comm` avec la chaîne et la racine ISRG X1, et redémarre le proxy
   et le MTA ;
4. écrit le résultat dans `status.json`. Il part en alerte `certificate_renewed` ou
   `certificate_renew_failed`.

Une tentative automatique au plus toutes les 6 heures. Certbot ne renouvelle qu'à moins
de 30 jours de l'expiration et utilise la méthode de validation choisie à la création
du certificat. Le port 80 du serveur doit donc être joignable depuis Internet pour la
validation HTTP.

Vérifier sur le serveur Carbonio :

```bash
systemctl is-enabled carborewind-cert.path
```

```bash
sudo cat /var/lib/carborewind/cert/status.json
```
