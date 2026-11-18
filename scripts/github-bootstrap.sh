#!/usr/bin/env bash
# Configure les réglages GitHub de CarboRewind que l'on ne peut pas versionner dans le dépôt :
# labels, jalons, protection des branches, projet kanban.
#
# Prérequis : gh >= 2.40 authentifié avec un compte administrateur du dépôt
#   gh auth login --scopes "repo,project"
# Usage :
#   scripts/github-bootstrap.sh [owner/repo]
# Le script est idempotent : il peut être relancé sans effet de bord.
set -euo pipefail

REPO="${1:-hooo86/CarboRewind}"
OWNER="${REPO%%/*}"

say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }

# ---------------------------------------------------------------------------
say "Labels"
labels=(
  "bug|d73a4a|Dysfonctionnement"
  "enhancement|a2eeef|Nouvelle fonctionnalité"
  "security|b60205|Sécurité"
  "documentation|0075ca|Documentation"
  "dependencies|0366d6|Mise à jour de dépendances"
  "triage|fbca04|À qualifier"
  "data-loss-risk|5c0000|Risque de perte de données"
  "carbonio-ce|1d76db|Spécifique Carbonio CE"
  "carbonio-advanced|5319e7|Spécifique Carbonio Advanced"
  "backend|c5def5|Composant backend"
  "agent|c5def5|Composant agent"
  "frontend|c5def5|Composant frontend"
  "deploy|c5def5|Déploiement"
  "ci|c5def5|Intégration continue"
)
for entry in "${labels[@]}"; do
  IFS='|' read -r name color desc <<<"$entry"
  gh label create "$name" --repo "$REPO" --color "$color" --description "$desc" --force >/dev/null
  echo "  ✓ $name"
done

# ---------------------------------------------------------------------------
say "Jalons"
milestones=(
  "M0 – Socle|Dépôt, CI/CD, sécurité de la chaîne, squelettes"
  "M1 – Backend cœur|Modèle de données, API OpenAPI, auth, RBAC, audit"
  "M2 – Agent & sauvegarde|mTLS, client Carbonio, dépôt chiffré, full/incrémentale, reprise"
  "M3 – Restauration|Navigation, recherche, restauration compte/dossier/élément, dry-run"
  "M4 – Frontend|Tableau de bord, jobs temps réel, explorateur, assistant de restauration"
  "M5 – v0.1.0|Planification, rétention, config, notifications, install, docs, release"
)
existing="$(gh api "repos/$REPO/milestones?state=all&per_page=100" --jq '.[].title')"
for entry in "${milestones[@]}"; do
  IFS='|' read -r title desc <<<"$entry"
  if grep -Fxq "$title" <<<"$existing"; then
    echo "  = $title (existe déjà)"
  else
    gh api "repos/$REPO/milestones" -f title="$title" -f description="$desc" >/dev/null
    echo "  ✓ $title"
  fi
done

say "Rattachement des issues « [Mx] … » à leur jalon"
for n in 0 1 2 3 4 5; do
  title="$(grep -F "M$n –" <<<"$(printf '%s\n' "${milestones[@]%%|*}")")"
  gh issue list --repo "$REPO" --state open --limit 200 --search "\"[M$n]\" in:title no:milestone" \
    --json number --jq '.[].number' | while read -r issue; do
      gh issue edit "$issue" --repo "$REPO" --milestone "$title" >/dev/null
      echo "  ✓ #$issue → $title"
    done
done

# ---------------------------------------------------------------------------
say "Réglages du dépôt (squash/rebase uniquement, suppression des branches fusionnées)"
gh api -X PATCH "repos/$REPO" \
  -F allow_merge_commit=false -F allow_squash_merge=true -F allow_rebase_merge=true \
  -F delete_branch_on_merge=true -F allow_auto_merge=true >/dev/null
gh api -X PUT "repos/$REPO/private-vulnerability-reporting" >/dev/null 2>&1 || true
gh api -X PUT "repos/$REPO/vulnerability-alerts" >/dev/null 2>&1 || true

# ---------------------------------------------------------------------------
say "Protection des branches main et develop"
protect() {
  local branch="$1" reviews="$2"
  gh api -X PUT "repos/$REPO/branches/$branch/protection" --input - >/dev/null <<JSON
{
  "required_status_checks": {
    "strict": true,
    "contexts": [
      "backend (lint, types, tests)",
      "agent (lint, types, tests)",
      "frontend (lint, types, tests, build)",
      "secret scan (gitleaks)"
    ]
  },
  "enforce_admins": true,
  "required_pull_request_reviews": {
    "required_approving_review_count": $reviews,
    "dismiss_stale_reviews": true
  },
  "restrictions": null,
  "required_linear_history": true,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "required_conversation_resolution": true
}
JSON
  echo "  ✓ $branch (revues requises : $reviews)"
}
# Avec un seul mainteneur, GitHub n'autorise pas l'auto-approbation : mettre 0 sur develop
# tant que l'équipe ne compte qu'une personne, et 1 sur main (fusion via un second compte
# ou en levant temporairement la règle). Ajuster selon l'équipe.
protect main 1
protect develop 0

# ---------------------------------------------------------------------------
say "Projet kanban"
if gh project list --owner "$OWNER" --format json --jq '.projects[].title' | grep -Fxq "CarboRewind"; then
  echo "  = projet CarboRewind (existe déjà)"
else
  gh project create --owner "$OWNER" --title "CarboRewind" >/dev/null
  echo "  ✓ projet CarboRewind créé — liez-le au dépôt : Projects → Link a project"
fi

say "Terminé."
