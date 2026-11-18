# Contribuer à CarboRewind / Contributing

Merci de votre intérêt ! / Thanks for your interest!

## Flux de travail / Workflow

- `main` : branche stable et protégée. Aucun push direct. / stable, protected, no direct push.
- `develop` : branche d'intégration. / integration branch.
- `feature/<sujet>`, `fix/<sujet>` : partent de `develop` et y reviennent par pull request.
- `release/vX.Y.Z` : préparation d'une version, fusionnée dans `main` puis taguée.

Une fonctionnalité = une branche = une PR, liée à son issue (`Closes #N`).

## Commits

Format [Conventional Commits](https://www.conventionalcommits.org/) :

```
feat(agent): add SyncRequest-based incremental backup
fix(api): reject restore targets outside caller's domain
docs: add DR runbook
```

Types : `feat`, `fix`, `docs`, `chore`, `test`, `refactor`, `perf`, `ci`, `build`, `security`.
Commits petits et atomiques. Historique linéaire (rebase / squash).

## Versionnage

[SemVer](https://semver.org/) ; tags `vX.Y.Z` ; la release GitHub et le changelog sont générés par la CI.

## Qualité

Avant d'ouvrir une PR :

```bash
# PostgreSQL + Redis jetables pour les tests d'intégration du backend (obligatoires :
# sans eux la plupart des tests sont ignorés et le seuil de couverture n'est pas atteint)
docker run -d --name cr-pg -p 5432:5432 -e POSTGRES_USER=carborewind \
  -e POSTGRES_PASSWORD=dev-only -e POSTGRES_DB=carborewind_test postgres:16-alpine
docker run -d --name cr-redis -p 6379:6379 redis:7-alpine
export CARBOREWIND_TEST_DATABASE_URL=postgresql+psycopg://carborewind:dev-only@localhost/carborewind_test
export CARBOREWIND_TEST_REDIS_URL=redis://localhost:6379/0

# backend / agent
cd backend && pip install -e '.[dev]' && ruff check . && ruff format --check . && mypy && pytest
python tools/gen_docs.py   # après une modification des modèles, des routes ou du RBAC
cd agent   && pip install -e '.[dev]' && ruff check . && ruff format --check . && mypy && pytest
# frontend
cd frontend && npm ci && npm run lint && npm run typecheck && npm test && npm run build
```

- Code typé, testé (objectif de couverture > 80 %), commenté là où c'est utile.
- **Aucun secret** dans le code ni dans l'historique (gitleaks tourne en CI).
- **Aucune commande shell construite à partir d'une donnée utilisateur.**
- Toute intégration Carbonio documente les API/commandes utilisées et les limites CE vs Advanced.

## Code de conduite

Voir [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
