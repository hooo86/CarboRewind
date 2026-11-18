# Intégration Carbonio / Carbonio integration

> Issue #7. Implémentation : `agent/src/carborewind_agent/carbonio/`.
> Édition ciblée : **Carbonio CE**. La dernière version CE confirmée par la documentation
> officielle au moment de l'écriture est la **25.12.0**. La version exacte du serveur cible n'a pas
> encore été communiquée.

## Statut de vérification

La documentation officielle `docs.zextras.com` n'était pas accessible depuis l'environnement de
développement. Chaque point porte donc l'un de ces statuts :

| Symbole | Sens |
|---|---|
| ✅ | confirmé par une source (référence API Zextras, documentation ou forum officiel Zextras) |
| 🔎 | **hypothèse** fondée sur l'API SOAP de Zimbra dont Carbonio dérive, à confirmer avec `probe` |

### Vérifier sur votre serveur (lecture seule)

```bash
sudo -u carborewind carborewind-agent carbonio probe --account test@example.org
```

La commande s'authentifie, liste un compte, obtient un jeton délégué, lance une synchronisation
initiale puis incrémentale, télécharge un message et lit ses métadonnées. **Rien n'est modifié** sur
le serveur, et aucun contenu de mail n'est affiché : seules les tailles le sont. Chaque étape
affiche ✓ ou ✗ avec le code d'erreur Carbonio. Merci de transmettre la sortie : elle permettra de
passer les 🔎 en ✅.

## Appels utilisés

| Besoin | Appel | Service / port | Statut |
|---|---|---|---|
| Authentification admin | `AuthRequest` (`urn:zimbraAdmin`) | `https://localhost:7071/service/admin/soap` | ✅ endpoint et port |
| Agir sur une boîte | `DelegateAuthRequest` (`account by=id`, `duration`) → jeton valable pour le compte cible | admin 7071 | ✅ |
| Inventaire paginé | `SearchDirectoryRequest` (`types=accounts`, `limit`, `offset`, `domain`, `attrs`) → `account[]`, `more` | admin 7071 | 🔎 attributs de pagination |
| Attributs d'un compte | `GetAccountRequest` (`account by=id`) → `a[] {n, _content}` | admin 7071 | 🔎 |
| Détection des changements | `SyncRequest` (`urn:zimbraMail`, `token` absent lors du premier appel, `typed`, `changeLimit`, `deleteLimit`) | `https://localhost/service/soap` | ✅ (sauvegarde complète réussie sur un serveur CE de production) |
| Arborescence initiale | réponse de `SyncRequest` sans token : `folder[]` imbriqués, chacun avec `m/cn/appt/task/doc: [{ids: "1,2"}]` | — | ✅ |
| Deltas | `m[]`, `cn[]`… modifiés, `deleted: [{ids: "…"}]`, `token`, `more` | — | 🔎 |
| Jeton trop ancien | erreur `mail.MUST_RESYNC`, qui déclenche une sauvegarde complète du compte | — | 🔎 |
| Contenu brut | REST `GET /home/<compte>/?id=<n>` avec le cookie `ZM_AUTH_TOKEN` (jeton délégué) | 443 | ✅ |
| Métadonnées | `GetItemRequest` (`item id`) → `f` (drapeaux), `tn` (tags), `l` (dossier), `d` (date) | 443 | ✅ |
| Arborescence actuelle (restauration) | `GetFolderRequest` (`folder {l: "1"}`) → `folder[]` imbriqués | 443 | ✅ |
| Existence d'un élément (mode « dossiers d'origine ») | `GetItemRequest` ; `mail.NO_SUCH_ITEM` ⇒ à restaurer | 443 | 🔎 code d'erreur |
| Restauration de dossier | `CreateFolderRequest` (`l`, `name`, `view`, `fie=1` : idempotent) | 443 | ✅ (restauration réussie sur un serveur CE de production) |
| Restauration de mail | `AddMsgRequest` (`m {l, f, tn, d, content}`). Contenu 8 bits : envoi préalable à `/service/upload?fmt=raw`, puis `aid` | 443 | ✅ `AddMsgRequest` ; 🔎 réponse du servlet d'upload `200,'null','<aid>'` (mails non UTF-8) |
| Import contacts et agendas | REST `POST /home/<compte>/<dossier>?fmt=ics|vcf&resolve=skip` | 443 | ✅ (forum Zextras : `postRestURL "/?fmt=tgz&resolve=skip"`) ; 🔎 contenu renvoyé par `?id=` pour un rendez-vous (iCalendar attendu) |
| Fichiers (Files / `doc`) | non restaurables par ce chemin en CE : signalés « non pris en charge » | — | limite connue |
| Export complet de contrôle | REST `GET /home/<compte>/?fmt=tgz` | 443 | ✅ |

Format d'enveloppe JSON 🔎 :

```json
{"Header": {"context": {"_jsns": "urn:zimbra", "authToken": "…", "format": {"type": "js"}}},
 "Body":   {"SyncRequest": {"_jsns": "urn:zimbraMail", "token": "1234", "typed": 1}}}
```

Une erreur est renvoyée avec le statut HTTP 500 :
`{"Body": {"Fault": {"Reason": {"Text": "…"}, "Detail": {"Error": {"Code": "mail.MUST_RESYNC"}}}}}`.

## Points d'attention connus (forums Zextras)

- **REST : connexion refusée ou erreur 404** sur une installation fraîche lorsque
  `zimbraPublicServiceHostname`, le protocole ou le port public du domaine sont incomplets. Il faut
  vérifier ces attributs, puis vider le cache du domaine. L'agent appelle `localhost` directement, ce
  qui limite le problème, mais `probe` le détectera.
- **Pas d'import/export dans l'interface web CE** : seules la CLI et l'API sont disponibles.

## Compte de service et moindre privilège

- En CE, la délégation fine des droits d'administration est limitée. L'agent utilise un **compte
  administrateur dédié** (`carborewind@…`), jamais le compte `admin` principal.
- Son mot de passe est lu dans `/etc/carborewind/carbonio-admin.password`, en `0600`, et le fichier est
  refusé s'il est lisible par le groupe ou par les autres utilisateurs. Il n'apparaît jamais dans la
  configuration, ni dans les journaux.
- Les jetons délégués ont une durée courte (1 h, renouvelée avant expiration) et sont mis en cache
  par compte. Carbonio enregistre chaque délégation dans ses propres journaux.
- **TLS toujours vérifié.** Le certificat de Carbonio est souvent auto-signé : fournissez sa CA
  via `CARBOREWIND_AGENT_CARBONIO_CA_FILE` plutôt que de désactiver la vérification, ce qui n'est pas
  possible.
- Les appels sont relancés sur erreur réseau ou sur 502, 503 et 504, avec un délai exponentiel. Les
  erreurs métier (`account.*`, `mail.*`) ne sont pas relancées.

## CE vs Advanced

| | Carbonio CE (ce module) | Carbonio Advanced (après le MVP) |
|---|---|---|
| Source des sauvegardes | API SOAP et REST décrites ci-dessus | module Backup natif (SmartScan, Real Time Scan) |
| Incrémental | `SyncRequest` + token, avec un grain de l'ordre de l'élément | journal temps réel du module |
| Restauration granulaire | `AddMsgRequest`, `CreateFolderRequest`, import REST | commandes de restauration du module |
| Intégration CarboRewind | `CarbonioClient` | futur `CarbonioAdvancedProvider` derrière la même interface |

## Simulateur

`agent/tests/fake_carbonio.py` implémente **exactement ce contrat** : séquences de changement,
tokens, tombstones et `MUST_RESYNC`, pagination `more`, faute SOAP, upload et REST. Il sert aux tests
contractuels du client et servira aux tests de bout en bout (#20). Si `probe` révèle un écart sur
votre version, on corrige le client **et** le simulateur dans la même PR.

## Sources

- Référence API Zextras, `DelegateAuth` et `Sync` (noms des requêtes, service, jeton) : https://docs.zextras.com/apidoc/api-reference/zimbraAdmin/DelegateAuth.html, https://docs.zextras.com/apidoc/api-reference/Mail/Sync.html
- Authentification SOAP Carbonio : https://docs.zextras.com/carbonio/html/develop/soap-api-auth.html
- Export et import REST `?fmt=tgz` sur Carbonio CE (forum Zextras) : https://community.zextras.com/forum/carbonio-general-thread/carbonio-ce-data-export-import, https://community.zextras.com/forum/carbonio-setup/error-service-failure-system-failure-post-failed-status500-500
- Versions CE : https://docs.zextras.com/carbonio-ce/html/release/changelog.html
