# Connexions échouées, IP bloquées et origine du spam

## Ce que l'agent lit

| Source | Ligne reconnue | Compté comme |
|---|---|---|
| `/opt/zextras/log/mailbox.log` | `authentication failed for [compte]` (IP = `oip=` du contexte, sinon `ip=`) | échec webmail / IMAP / POP3 / ActiveSync |
| `/opt/zextras/log/mailbox.log` | `Access to IP 203.0.113.9 suspended, for repeated failed login` | IP bloquée par Carbonio |
| journal MTA (`/var/log/carbonio.log`) | `warning: hôte[IP]: SASL LOGIN authentication failed` | échec d'envoi SMTP authentifié |

Les compteurs sont agrégés par heure et par IP, et envoyés au serveur chaque minute avec les
statistiques de trafic. Ils sont conservés 90 jours. Les adresses de boucle locale (le proxy)
sont ignorées.

Le blocage est fait par le filtre de connexions invalides de Carbonio (attributs
`zimbraInvalidLoginFilter*`, voir `zmprov gacf | grep -i InvalidLoginFilter`). CarboRewind
**n'en bloque ni n'en débloque aucune** : il affiche ce que Carbonio a fait. Pour qu'une adresse
légitime (NAT de l'entreprise, supervision) ne soit plus bloquée, ajoutez-la à
`zimbraHttpThrottleSafeIPs` puis redémarrez le service mailbox.

## Débloquer une adresse

Le bouton **Débloquer** demande à l'agent d'ajouter l'adresse à `zimbraHttpThrottleSafeIPs`
(`ModifyConfigRequest`, valeur ajoutée avec `+`) : elle n'est plus jamais bloquée.
**Retirer la confiance** l'enlève (`-`). Les adresses et réseaux de confiance sont listés
sous « Adresses de confiance » ; l'agent relit la liste et la règle de blocage toutes les
10 minutes, et juste après chaque modification.

Carbonio garde les blocages en cours en mémoire, sans commande pour en lever un : un blocage
se lève à la fin du délai (`zimbraInvalidLoginFilterDelayInMinBetwnReqBeforeReinstating`),
ou au redémarrage du service mailbox, qui peut aussi être nécessaire pour que la nouvelle liste
soit prise en compte :

```bash
su - zextras -c "zmmailboxdctl restart"
```

« bloquée » (rouge) = blocage dans la dernière heure ; « bloquée N fois » = blocages plus anciens
sur la période.

## Droits nécessaires

L'agent doit pouvoir lire `mailbox.log` :

```bash
sudo usermod -aG zextras carborewind && sudo systemctl restart carborewind-agent
```

Sans ce droit, l'agent le signale dans son journal et la page affiche « Aucune donnée reçue ».

## Origine du spam

Pour chaque spam reçu, Amavis journalise l'IP du serveur qui l'a remis. Le serveur CarboRewind
résout le nom DNS inverse (PTR) de ces IP (2 s maximum par adresse, résultat gardé 24 h) et
les regroupe par domaine (`mail-1.mx.exemple.co.uk` → `exemple.co.uk`). Les domaines des
adresses d'expéditeur sont listés à côté : ils sont facilement falsifiés, le domaine de l'IP
l'est beaucoup moins.

## API

- `GET /api/mail/security?period=24h&only_blocked=false&limit=100`
- `GET /api/mail/spam-sources?period=24h`
- `GET /api/mail/login-filter`, `POST /api/mail/login-filter/trust|untrust` `{"ip": "203.0.113.9"}`
- `GET /api/me/dashboard`, `PUT /api/me/dashboard` `{"widgets": [...] | null}`
