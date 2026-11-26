# Migrations IMAP vers Carbonio

CarboRewind peut copier des boîtes d'un autre serveur de messagerie (Zimbra, Dovecot, Cyrus,
cPanel, Exchange avec IMAP activé, Gmail avec mot de passe d'application…) vers des comptes
Carbonio. Menu **Migrations** (super-administrateurs, permission `migrations:manage`).

## Principe

1. **Serveur source** : hôte, port, chiffrement (SSL/TLS, STARTTLS ou aucun), vérification du
   certificat (activée par défaut) et mode d'authentification :
   - *mot de passe de chaque boîte* (fourni dans le fichier ou saisi) ;
   - *compte administrateur* : `AUTHENTICATE PLAIN` avec identité d'autorisation, l'administrateur
     lit la boîte sans connaître son mot de passe (Zimbra/Carbonio admin, Dovecot master user,
     Cyrus) ;
   - *utilisateur maître Dovecot* : connexion `boîte*maître` (séparateur configurable).
2. **Lot** : liste de boîtes et options — dossier racine de destination (sinon les dossiers
   d'origine), dossiers exclus, plage de dates.
3. **Boîtes** : import d'un fichier Excel/CSV, saisie manuelle (une ligne
   `source[;destination[;mot de passe]]`) et **association automatique** à un compte Carbonio.
4. **Test de connexion** (simulation : connexion, liste des dossiers, nombre de messages à copier),
   puis **copie**. Relancée, la copie ne prend que les nouveaux messages : faire une première
   copie, basculer les MX, puis une dernière passe rapide.

## Association automatique

Pour chaque adresse source, dans l'ordre :

| Règle | Exemple | Badge |
|---|---|---|
| destination indiquée (fichier ou saisie), adresse ou alias | `direction@ancien.dz` → `yacine@nouveau.dz` | choisie |
| même adresse | `ana@nouveau.dz` → `ana@nouveau.dz` | même adresse |
| alias d'un compte | `contact@ancien.dz` est un alias de `leila@nouveau.dz` | alias |
| même nom avant le `@` | `ana@ancien.dz` → `ana@nouveau.dz` | même nom |

Le **domaine de destination** limite la dernière règle (utile quand le même nom existe dans
plusieurs domaines). Une adresse sans correspondance unique reste « à choisir » ; le rapport
d'import liste les candidats. « Relancer l'association automatique » rattrape les comptes créés
depuis dans Carbonio. Une source peut être copiée vers plusieurs comptes et plusieurs sources
vers un même compte (champ *dossier* pour les séparer).

## Fichier Excel / CSV

Modèles téléchargeables depuis la page du lot. Colonnes (en-têtes FR ou EN, casse et accents
ignorés) :

| Colonne | Synonymes acceptés | Obligatoire |
|---|---|---|
| `source_email` | source, adresse source, email, mail | oui |
| `source_login` | login, identifiant, username | non (l'adresse par défaut) |
| `password` | mot de passe, mdp | si authentification par boîte |
| `destination` | target, cible, nouvelle adresse | non (association automatique) |
| `dossier` | folder, root | non |

Sans ligne d'en-tête, les colonnes sont lues dans l'ordre `source, destination, mot de passe,
identifiant, dossier`. Le fichier est lu en mémoire puis oublié (5 Mo, 5 000 lignes, 50 Mo
décompressés au plus). Chaque ligne invalide est signalée avec son numéro.

## Dossiers

- `INBOX` → `Inbox` ; dossiers marqués `\Sent`, `\Drafts`, `\Trash`, `\Junk` (RFC 6154) ou
  portant un nom connu (`Sent Items`, `Éléments envoyés`, `Corbeille`, `Spam`, `Courrier
  indésirable`…) → dossier système Carbonio correspondant ;
- préfixe d'espace de noms retiré (`INBOX.Projets` sur Courier → `/Projets`), séparateur → `/` ;
- dossiers virtuels Gmail (« Tous les messages », « Suivis ») ignorés ;
- drapeaux conservés : non lu, suivi, répondu, transféré, brouillon ; « envoyé » dans `Sent` ;
  date de réception d'origine (`INTERNALDATE`) ; messages marqués `\Deleted` non copiés.

## Reprise, incrémental, doublons

L'agent enregistre pour chaque dossier source son `UIDVALIDITY`, le dernier UID copié et les UID
en échec (retentés à la copie suivante). Une copie interrompue (arrêt, panne, annulation) reprend
au dernier point de contrôle. Si un dossier n'a pas d'état exploitable (première copie vers une
boîte qui contient déjà du courrier, ou `UIDVALIDITY` changé par le serveur source), chaque
message est d'abord cherché par `Message-ID` dans le dossier cible : pas de doublon.

## Sécurité

- Le serveur source n'est **jamais modifié** : dossiers ouverts en lecture seule (`EXAMINE`),
  messages lus avec `BODY.PEEK[]` (l'indicateur « lu » ne change pas).
- Mots de passe (boîtes et compte maître) chiffrés en base avec `CARBOREWIND_SECRET_KEY`
  (Fernet), jamais renvoyés par l'API, absents des paramètres de job et du journal d'audit ; ils
  ne quittent le serveur qu'à destination de l'agent, dans la réponse mTLS à sa demande de job.
  **Oublier les mots de passe** les efface une fois la migration terminée.
- Aucune commande shell : l'agent utilise `imaplib` (bibliothèque standard).
- Chaque action (import, modification, lancement, annulation) est tracée dans l'audit.

## Limites

- Courrier uniquement : contacts et agendas ne transitent pas par IMAP.
- Microsoft 365 / Exchange Online (OAuth2 obligatoire) ne sont pas pris en charge.
- Les comptes de destination doivent exister dans Carbonio.
- La recherche de doublons utilise `SearchRequest` avec `msgid:` (🔎 à confirmer avec
  `carborewind-agent carbonio probe` sur votre version).

## Notification

L'événement **Lot de migration terminé** (paramètres SMTP) envoie un résumé quand plus aucune
copie d'un lot n'est en attente ou en cours ; un job en échec définitif déclenche aussi
l'alerte « job en échec ».
