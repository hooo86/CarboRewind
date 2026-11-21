#!/usr/bin/env bash
# CarboRewind — installation from scratch of the AGENT on the Carbonio server.
#
#   sudo bash setup-agent.sh --api-url https://carborewind.example.org \
#        --deb carborewind-agent_<version>_amd64.deb [--api-ca server.crt] \
#        [--api-ip 192.0.2.10] [--carbonio-admin carborewind@example.org] \
#        [--repository /var/lib/carborewind/repository]
#
# What it does (each step is skipped when already done, so it can be re-run):
#   1. installs the agent package;
#   2. resolves the API name to --api-ip in /etc/hosts when given (VPN / split DNS);
#   3. creates the dedicated Carbonio admin account with a random password stored in a
#      0600 file (the password never appears on a command line or on screen);
#   4. writes /etc/carborewind/agent.env (API URL, CA files, account, repository);
#   5. creates the repository key and the encrypted repository;
#   6. checks the configuration, enrols the agent with the one-time token (typed at the
#      keyboard, not shown) and starts the service.
set -euo pipefail

API_URL=""
API_CA=""
API_IP=""
DEB=""
ADMIN=""
REPO=/var/lib/carborewind/repository
ETC=/etc/carborewind
ENV_FILE=$ETC/agent.env
PW_FILE=$ETC/carbonio-admin.password

die() { echo "error: $*" >&2; exit 1; }
say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
usage() { sed -n '2,18p' "$0"; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --api-url) API_URL="${2:-}"; shift 2 ;;
        --api-ca) API_CA="${2:-}"; shift 2 ;;
        --api-ip) API_IP="${2:-}"; shift 2 ;;
        --deb) DEB="${2:-}"; shift 2 ;;
        --carbonio-admin) ADMIN="${2:-}"; shift 2 ;;
        --repository) REPO="${2:-}"; shift 2 ;;
        -h|--help) usage 0 ;;
        *) echo "unknown option $1" >&2; usage 2 ;;
    esac
done

[ "$(id -u)" = 0 ] || die "run as root (sudo)"
[[ "$API_URL" =~ ^https://([A-Za-z0-9.-]+)(:[0-9]+)?/?$ ]] \
    || die "--api-url must look like https://carborewind.example.org"
API_HOST="${BASH_REMATCH[1]}"
[ -z "$API_CA" ] || [ -r "$API_CA" ] || die "--api-ca: $API_CA is not readable"
[ -z "$API_IP" ] || [[ "$API_IP" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || die "--api-ip must be an IPv4 address"
[[ "$REPO" == /* ]] || die "--repository must be an absolute path"
id zextras >/dev/null 2>&1 || die "no zextras user: run this on the Carbonio server"
zx() { su - zextras -c "$1"; }

# ------------------------------------------------------------------ 1. package
if [ -n "$DEB" ]; then
    [ -r "$DEB" ] || die "--deb: $DEB is not readable"
    say "installing $(basename "$DEB")"
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$(realpath "$DEB")" >/dev/null
fi
command -v carborewind-agent >/dev/null || die "carborewind-agent is not installed: give --deb"

# ------------------------------------------------------------------ 2. name of the API
if [ -n "$API_IP" ]; then
    current="$(getent ahostsv4 "$API_HOST" | awk 'NR==1{print $1}')"
    if [ "$current" != "$API_IP" ]; then
        say "$API_HOST resolves to ${current:-nothing}: adding '$API_IP $API_HOST' to /etc/hosts"
        sed -i "/[[:space:]]$API_HOST\$/d" /etc/hosts
        echo "$API_IP $API_HOST" >> /etc/hosts
    fi
fi
getent hosts "$API_HOST" >/dev/null || die "$API_HOST does not resolve: give --api-ip"

# ------------------------------------------------------------------ 3. Carbonio account
if [ -z "$ADMIN" ]; then
    domain="$(zx "zmprov gcf zimbraDefaultDomainName" | awk '{print $2}')"
    [ -n "$domain" ] || die "no default domain found: give --carbonio-admin"
    ADMIN="carborewind@$domain"
fi
[[ "$ADMIN" =~ ^[A-Za-z0-9._-]+@[A-Za-z0-9.-]+$ ]] || die "--carbonio-admin: invalid address"
[ -d "$ETC" ] || die "$ETC missing: the package did not install correctly"
new_password() { openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | head -c 40; }
if zx "zmprov -l ga $ADMIN zimbraId" >/dev/null 2>&1; then
    if [ -s "$PW_FILE" ]; then
        say "Carbonio account $ADMIN and its password file already exist: kept"
    else
        say "account $ADMIN exists without a password file: giving it a new random password"
        pw="$(new_password)"
        printf 'sp %s %s\n' "$ADMIN" "$pw" | zx "zmprov" >/dev/null
        install -o carborewind -g carborewind -m 0600 /dev/null "$PW_FILE"
        printf '%s' "$pw" > "$PW_FILE"; unset pw
    fi
else
    say "creating the dedicated Carbonio admin account $ADMIN (random password)"
    pw="$(new_password)"
    # Commands fed to zmprov on stdin: the password never shows in the process list.
    printf 'ca %s %s zimbraIsAdminAccount TRUE displayName CarboRewind\n' "$ADMIN" "$pw" \
        | zx "zmprov" >/dev/null
    install -o carborewind -g carborewind -m 0600 /dev/null "$PW_FILE"
    printf '%s' "$pw" > "$PW_FILE"; unset pw
fi

# ------------------------------------------------------------------ 4. agent.env
set_env() {  # set_env KEY VALUE: uncomment / replace / append
    if grep -qE "^#?$1=" "$ENV_FILE"; then
        sed -i -E "s|^#?$1=.*|$1=$2|" "$ENV_FILE"
    else
        echo "$1=$2" >> "$ENV_FILE"
    fi
}
say "writing $ENV_FILE"
set_env CARBOREWIND_AGENT_API_URL "$API_URL"
set_env CARBOREWIND_AGENT_CARBONIO_ADMIN_NAME "$ADMIN"
set_env CARBOREWIND_AGENT_CARBONIO_ADMIN_PASSWORD_FILE "$PW_FILE"
set_env CARBOREWIND_AGENT_REPOSITORY_PATH "$REPO"
if [ -n "$API_CA" ]; then
    install -m 0644 "$API_CA" "$ETC/api-ca.crt"
    set_env CARBOREWIND_AGENT_API_CA_FILE "$ETC/api-ca.crt"
fi
# Carbonio's own certificate: self-signed by its internal CA on most installations.
if ! curl -fs -o /dev/null https://localhost:7071/ 2>/dev/null; then
    for ca in /opt/zextras/conf/ca/ca.pem /opt/zextras/ssl/carbonio/ca/ca.pem; do
        if [ -r "$ca" ]; then
            say "Carbonio uses its own CA: trusting $ca"
            install -m 0644 "$ca" "$ETC/carbonio-ca.crt"
            set_env CARBOREWIND_AGENT_CARBONIO_CA_FILE "$ETC/carbonio-ca.crt"
            break
        fi
    done
fi

# ------------------------------------------------------------------ 5. repository
install -d -o carborewind -g carborewind -m 0700 "$REPO"
if [ ! -s "$ETC/repository.key" ]; then
    say "creating the repository key $ETC/repository.key"
    carborewind-agent repo keygen
    KEY_CREATED=1
fi
if [ -z "$(ls -A "$REPO" 2>/dev/null)" ]; then
    say "initialising the encrypted repository in $REPO"
    carborewind-agent repo init
else
    say "repository $REPO already initialised: kept"
fi

# ------------------------------------------------------------------ 6. check, enrol, start
say "checking the configuration"
carborewind-agent check-config
if [ ! -s /var/lib/carborewind/tls/agent.crt ]; then
    say "enrolment: paste the one-time token printed on the CarboRewind server (not shown)"
    read -r -s -p "token: " token; echo
    [ -n "$token" ] || die "no token given"
    printf '%s\n' "$token" | carborewind-agent enroll
    unset token
else
    say "agent already enrolled: kept"
fi
systemctl enable --now carborewind-agent
sleep 5
systemctl is-active --quiet carborewind-agent || die "the agent did not start: journalctl -u carborewind-agent"

cat <<EOF

────────────────────────────────────────────────────────────────────────
 CarboRewind agent running (journal: journalctl -u carborewind-agent -f)
────────────────────────────────────────────────────────────────────────
EOF
if [ "${KEY_CREATED:-0}" = 1 ]; then
    cat <<EOF
 IMPORTANT: copy the repository key NOW into two places outside this server
 (team password vault + a sealed envelope). Without it the backups cannot be read:
     sudo cat $ETC/repository.key
EOF
fi
echo " The accounts appear in the CarboRewind interface within a few minutes."
