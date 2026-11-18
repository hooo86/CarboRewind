# carborewind-agent

Agent exécuté sur le serveur Carbonio (service systemd). Il récupère les jobs auprès de l'API
(connexion sortante mTLS), dialogue en local avec Carbonio (SOAP Admin, REST, LDAP) et écrit
directement dans le dépôt de sauvegarde (local/NFS).

```bash
pip install -e '.[dev]'
carborewind-agent --version
pytest
```
