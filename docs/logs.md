# Journaux dans l'interface

Menu **Configuration → Journaux**, réservé aux super-administrateurs. Lecture seule ; chaque
lecture est consignée dans le journal d'audit (`logs.read`).

| Source | Origine | Comment c'est lu |
|---|---|---|
| CarboRewind — API | conteneur `api` | tampon Redis `carborewind:logs:api` (5000 dernières lignes) |
| CarboRewind — planificateur | conteneur `scheduler` | tampon Redis `carborewind:logs:scheduler` |
| CarboRewind — agent | serveur Carbonio | `journalctl -u carborewind-agent`, via un job de lecture |
| Carbonio — messagerie | serveur Carbonio | `/var/log/carbonio.log` (ou `zimbra.log`/`mail.log`) |
| Carbonio — boîtes | serveur Carbonio | `/opt/zextras/log/mailbox.log` |

Les journaux du serveur Carbonio sont lus par l'agent : l'interface crée un job, l'agent renvoie
les N dernières lignes (2000 au plus), le texte est gardé une heure puis effacé. Aucune commande
n'est construite à partir de ce que saisit l'utilisateur : la source est choisie dans une liste
fixe, le chemin et la commande sont figés dans l'agent.

## Droits sur le serveur Carbonio

L'utilisateur système `carborewind` doit pouvoir lire ces fichiers :

```bash
sudo usermod -aG adm,systemd-journal,zextras carborewind && sudo systemctl restart carborewind-agent
```

## API

- `GET /api/logs/server?process=api|scheduler&lines=500`
- `POST /api/logs/agent` `{"source": "agent|mail|mailbox", "lines": 500}` → `{"job_id"}`
- `GET /api/logs/agent/{job_id}` → `{"status", "error", "text"}`
