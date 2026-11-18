#!/bin/sh
# /usr/bin/carborewind-agent — runs the agent with its bundled Python (-I: isolated mode,
# ignores PYTHON* variables and the current directory).
# Interactive commands launched as root are re-executed as the service user so that the files
# they create (keys, repository, certificates) belong to it.
set -e
PY=/opt/carborewind-agent/python/bin/python3
if [ -r /etc/carborewind/agent.env ]; then
    set -a
    # shellcheck disable=SC1091
    . /etc/carborewind/agent.env
    set +a
fi
if [ "$(id -u)" = "0" ]; then
    exec setpriv --reuid=carborewind --regid=carborewind --init-groups \
        "$PY" -I -m carborewind_agent "$@"
fi
exec "$PY" -I -m carborewind_agent "$@"
