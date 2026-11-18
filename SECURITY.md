# Politique de sécurité / Security Policy

## Versions prises en charge / Supported versions

| Version | Supportée |
|---|---|
| `main` (pré-0.1.0) | ✅ |

## Signaler une vulnérabilité / Reporting a vulnerability

**N'ouvrez pas d'issue publique.** Utilisez le signalement privé GitHub :
*Security → Report a vulnerability* (GitHub Private Vulnerability Reporting) sur ce dépôt.

**Do not open a public issue.** Use GitHub Private Vulnerability Reporting
(*Security → Report a vulnerability*) on this repository.

Indiquez : version/commit, composant (backend, agent, frontend, déploiement), étapes de reproduction, impact.
Nous accusons réception sous 5 jours ouvrés et visons un correctif sous 90 jours (divulgation coordonnée).

## Principes / Principles

- Aucun secret dans le dépôt ; secrets via variables d'environnement, fichiers montés ou GitHub Secrets.
- Données de sauvegarde chiffrées au repos (AES-256-GCM) et en transit (TLS / mTLS agent ↔ API).
- Moindre privilège et séparation des rôles (RBAC).
- Journal d'audit chaîné (tamper-evident).
- Analyse automatique en CI : CodeQL, scan des dépendances, détection de secrets (gitleaks).
