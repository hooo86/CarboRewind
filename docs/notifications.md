# Notifications par e-mail

Menu **Paramètres** (super-administrateurs) → *Notifications par e-mail (SMTP)*.

- Serveur, port et chiffrement : STARTTLS (587), SSL/TLS implicite (465) ou aucun (25, déconseillé). Le
  certificat du serveur SMTP est vérifié.
- Identifiant et mot de passe : le mot de passe est **chiffré** en base avec `CARBOREWIND_SECRET_KEY`
  (générée par `install-server.sh`) et n'est jamais renvoyé par l'API.
- Expéditeur, destinataires, langue des messages (FR/EN).
- Événements : job en échec définitif, sauvegarde partielle, restauration incomplète, agent sans contact
  depuis 30 min, résumé quotidien des comptes sans sauvegarde réussie depuis 24 h (heure et fuseau réglables).
- **Envoyer un e-mail de test** vérifie la configuration immédiatement et affiche l'erreur éventuelle.

Les messages sont mis en file d'attente dans la même transaction que l'événement, puis envoyés par le service
`scheduler`, avec jusqu'à 6 tentatives (délais croissants). La liste **Derniers messages** montre l'état de
chaque envoi. Le conteneur `scheduler` dispose pour cela d'un réseau sortant (`egress`).


## Serveur SMTP de secours

**Paramètres → Notifications → Serveur SMTP de secours** : quand le serveur principal refuse ou
ne répond pas, chaque alerte repart aussitôt par le serveur de secours (son propre hôte, port,
chiffrement, identifiant, mot de passe chiffré, et expéditeur facultatif). Le journal d'envoi
indique « sent by the fallback server » avec l'erreur du serveur principal ; si les deux
échouent, le message est retenté plus tard avec les deux erreurs. Un bouton envoie un e-mail de
test par le serveur de secours.
