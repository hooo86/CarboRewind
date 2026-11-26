#!/usr/bin/env bash
# CarboRewind — installation from scratch of the SERVER on a fresh Ubuntu 22.04 / 24.04.
#
#   sudo ./deploy/bootstrap-server.sh --server-name carborewind.example.org \
#        --carbonio-host mail.example.org [--agent-name mx1] [--admin admin] \
#        [--tls-cert fullchain.pem --tls-key privkey.pem] [--build-agent]
#
# What it does, in order (each step is skipped when already done, so it can be re-run):
#   1. installs Docker Engine + Compose v2 (official Docker repository), git, curl, openssl;
#   2. runs deploy/install-server.sh (secrets, TLS, images, database, agents' CA, health);
#   3. creates the first super-administrator (password typed at the keyboard, never shown);
#   4. registers the agent of the Carbonio server and prints its one-time token (1 h);
#   5. with --build-agent: builds carborewind-agent_<version>_amd64.deb into ./dist.
#
# No password is ever passed on a command line or written to a log.
set -euo pipefail

SERVER_NAME=""
CARBONIO_HOST=""
AGENT_NAME="mx1"
ADMIN_USER="admin"
TLS_ARGS=()
BUILD_AGENT=0

die() { echo "error: $*" >&2; exit 1; }
say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
usage() { sed -n '2,17p' "$0"; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --server-name) SERVER_NAME="${2:-}"; shift 2 ;;
        --carbonio-host) CARBONIO_HOST="${2:-}"; shift 2 ;;
        --agent-name) AGENT_NAME="${2:-}"; shift 2 ;;
        --admin) ADMIN_USER="${2:-}"; shift 2 ;;
        --tls-cert) TLS_ARGS+=(--tls-cert "${2:-}"); shift 2 ;;
        --tls-key) TLS_ARGS+=(--tls-key "${2:-}"); shift 2 ;;
        --build-agent) BUILD_AGENT=1; shift ;;
        -h|--help) usage 0 ;;
        *) echo "unknown option $1" >&2; usage 2 ;;
    esac
done

[ "$(id -u)" = 0 ] || die "run as root (sudo)"
[ -n "$SERVER_NAME" ] || die "--server-name is required (e.g. carborewind.example.org)"
[ -n "$CARBONIO_HOST" ] || die "--carbonio-host is required (the Carbonio server's name)"
DNS='^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$'
[[ "$SERVER_NAME" =~ $DNS ]] || die "--server-name must be a DNS name"
[[ "$CARBONIO_HOST" =~ $DNS ]] || die "--carbonio-host must be a DNS name"
[[ "$AGENT_NAME" =~ ^[a-z0-9][a-z0-9_-]{0,62}$ ]] || die "--agent-name: lowercase letters, digits, - and _"
[[ "$ADMIN_USER" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$ ]] || die "--admin: invalid user name"

SRC="$(cd "$(dirname "$0")/.." && pwd)"
[ -x "$SRC/deploy/install-server.sh" ] || die "run this script from a CarboRewind source tree"
. /etc/os-release
[ "${ID:-}" = ubuntu ] || [ "${ID:-}" = debian ] || die "Ubuntu or Debian required (found ${ID:-unknown})"

# ------------------------------------------------------------------ 1. prerequisites
if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
    say "installing Docker Engine and Compose (official Docker repository)"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq ca-certificates curl gnupg openssl git >/dev/null
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL "https://download.docker.com/linux/$ID/gpg" -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/$ID ${VERSION_CODENAME} stable" > /etc/apt/sources.list.d/docker.list
    apt-get update -qq
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin \
        docker-compose-plugin >/dev/null
    systemctl enable --now docker
else
    say "Docker $(docker --version | awk '{print $3}' | tr -d ,) already installed"
    command -v openssl >/dev/null && command -v curl >/dev/null \
        || { apt-get update -qq && apt-get install -y -qq openssl curl >/dev/null; }
fi

# ------------------------------------------------------------------ 2. server stack
say "installing the CarboRewind server (install-server.sh)"
"$SRC/deploy/install-server.sh" --server-name "$SERVER_NAME" "${TLS_ARGS[@]}"
cd /opt/carborewind/deploy
COMPOSE=(docker compose -f docker-compose.prod.yml)

# ------------------------------------------------------------------ 3. first administrator
users="$("${COMPOSE[@]}" exec -T db psql -U carborewind -d carborewind -tAc \
    "select count(*) from users" 2>/dev/null || echo 0)"
if [ "${users//[^0-9]/}" -gt 0 ] 2>/dev/null; then
    say "an administrator already exists: skipped (reset a password with: carborewind-api set-password <name>)"
else
    say "creating the first super-administrator '$ADMIN_USER' (12 characters at least)"
    "${COMPOSE[@]}" run --rm api carborewind-api create-user "$ADMIN_USER"
fi

# ------------------------------------------------------------------ 4. agent registration
say "registering the agent '$AGENT_NAME' for $CARBONIO_HOST"
if ! "${COMPOSE[@]}" run --rm api carborewind-api agent-create "$AGENT_NAME" "$CARBONIO_HOST"; then
    echo "    (already registered: an agent name is used once. Re-run with another"
    echo "     --agent-name, e.g. ${AGENT_NAME}-2, to get a new one-time token)"
fi

# ------------------------------------------------------------------ 5. agent package
if [ "$BUILD_AGENT" = 1 ]; then
    say "building the agent package"
    if ! command -v uv >/dev/null && [ ! -x "$HOME/.local/bin/uv" ]; then
        curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null
    fi
    PATH="$HOME/.local/bin:$PATH" "$SRC/deploy/packaging/agent/build-deb.sh" "$SRC/dist"
    DEB="$(ls -t "$SRC"/dist/carborewind-agent_*_amd64.deb | head -1)"
fi

cat <<EOF

────────────────────────────────────────────────────────────────────────
 CarboRewind server ready: https://$SERVER_NAME
────────────────────────────────────────────────────────────────────────
 Copy to the Carbonio server ($CARBONIO_HOST), into /tmp:
   - $SRC/deploy/setup-agent.sh
   - /opt/carborewind/deploy/tls/server.crt      (certificate of this server)
EOF
[ -n "${DEB:-}" ] && echo "   - $DEB"
cat <<EOF
 then, on the Carbonio server:
   sudo bash /tmp/setup-agent.sh --api-url https://$SERVER_NAME --api-ca /tmp/server.crt \\
        --deb /tmp/$(basename "${DEB:-carborewind-agent_VERSION_amd64.deb}")
 and paste the one-time token printed above when asked (valid 1 hour).

 Keep a copy of /opt/carborewind/deploy/.env and of the Docker volume carborewind_pki
 outside this server (see docs/install.md, « À sauvegarder hors site »).
EOF
