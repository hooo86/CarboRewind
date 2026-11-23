#!/bin/sh
# /usr/bin/carborewind-agent — runs the agent with its bundled Python (-I: isolated mode,
# ignores PYTHON* variables and the current directory).
# Interactive commands launched as root are re-executed as the service user so that the files
# they create (keys, repository, certificates) belong to it. Exception: the storage commands
# that mount shares and write systemd units (storage smb / local / apply) and the start of
# Carbonio asked by the agent (control apply) and the certificate renewal (cert apply)
# need root and keep it.
set -e
PY=/opt/carborewind-agent/python/bin/python3
if [ -r /etc/carborewind/agent.env ]; then
    set -a
    # shellcheck disable=SC1091
    . /etc/carborewind/agent.env
    set +a
fi
if [ "$(id -u)" = "0" ]; then
    case "$1:${2:-}" in
        storage:smb|storage:local|storage:apply|storage:--env-file|control:apply|cert:apply)
            exec "$PY" -I -m carborewind_agent "$@" ;;
    esac
    exec setpriv --reuid=carborewind --regid=carborewind --init-groups \
        "$PY" -I -m carborewind_agent "$@"
fi
exec "$PY" -I -m carborewind_agent "$@"
