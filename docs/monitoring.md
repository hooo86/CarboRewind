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
