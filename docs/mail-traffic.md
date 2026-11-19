# Trafic mail : limites d'envoi, spams

Menu **Trafic mail** (super-administrateurs). L'agent lit le journal du MTA du serveur Carbonio
(`/var/log/carbonio.log`, à défaut `zimbra.log` ou `mail.log`) dans un fil d'exécution séparé des
sauvegardes, et envoie chaque minute des compteurs par heure (UTC) :

| Compteur | Source dans le journal |
|---|---|
| Messages et destinataires **envoyés** par boîte | `postfix/cleanup` (Message-ID) + `postfix/qmgr: from=<…>, nrcpt=N` ; un message compte une seule fois même réinjecté par l'antispam ; seuls les expéditeurs qui sont des comptes (ou alias) locaux sont comptés |
| **Spams reçus** par boîte | `amavis … Blocked/Passed SPAM|SPAMMY {…Inbound…}` |
| **Expéditeurs de spams** et leurs adresses IP | les mêmes lignes |
| **Spams émis** par une boîte locale | `amavis … ORIGINATING` / `…Outbound…` : signe typique d'un mot de passe volé |

Au premier démarrage, seule la fin du journal (20 Mo) est lue ; la rotation du journal est suivie.
L'agent lit le journal grâce au groupe `adm` (`SupplementaryGroups=adm` dans son service).

## Limites d'envoi

**Trafic mail → Limites d'envoi** : messages par heure, destinataires par heure, messages par jour,
destinataires par jour, spams émis par jour (0 = pas de limite). « Par heure » = heure en cours,
« par jour » = 24 dernières heures. Action au dépassement :

- **alerter** : alerte dans l'écran + e-mail « Limite d'envoi atteinte » (événement `mail_limit` des
  notifications) ;
- **alerter et verrouiller** : en plus, l'agent verrouille la boîte dans Carbonio dans la minute
  (`zimbraAccountStatus = locked` : plus de webmail, d'IMAP ni d'envoi authentifié).

Une alerte est créée une seule fois par boîte, mesure et période. Garde-fous : un compte
**administrateur** n'est jamais verrouillé automatiquement (alerte seulement), le compte technique
utilisé par l'agent jamais, et au plus 3 verrouillages automatiques par heure (réglable) ; au-delà,
alerte seulement, pour éviter une cascade en cas d'erreur. Les **boîtes exemptées** (listes
de diffusion, comptes applicatifs) ne sont jamais limitées. Une boîte peut aussi être verrouillée ou
déverrouillée à la main ; le déverrouillage n'est jamais automatique : changez d'abord le mot de
passe si la boîte a été piratée. Les messages déjà en file d'attente avant le verrouillage peuvent
encore partir (`postqueue -p` / `postsuper` sur le serveur Carbonio pour les retirer).

Les compteurs sont conservés 90 jours. Chaque verrouillage, déverrouillage et dépassement est tracé
dans le journal d'audit.

## Vérifier

```bash
sudo -u carborewind head -c 300 /var/log/carbonio.log   # l'agent peut-il lire le journal ?
sudo journalctl -u carborewind-agent | grep -i "mail statistics"
```

Si l'écran indique « Aucune statistique reçue », mettez à jour le paquet de l'agent (il ajoute le
groupe `adm`) et redémarrez-le. Si votre syslog écrit ailleurs, indiquez le chemin dans
`/etc/carborewind/agent.env` : `CARBOREWIND_AGENT_MAIL_LOGS=["/var/log/mail.log"]`.
