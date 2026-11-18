#!/usr/bin/env bash
# Build carborewind-agent_<version>_amd64.deb with a self-contained Python runtime.
#
# Usage: deploy/packaging/agent/build-deb.sh [output-dir]
#   PYTHON_DIR  path to a relocatable CPython (python-build-standalone), e.g. obtained with
#               `uv python install 3.12 --install-dir …`. Built for glibc 2.17+, so the
#               package runs on Ubuntu 20.04+/Debian 11+/RHEL 8+ (via alien/rpm, untested).
#   VERSION     package version (default: 0.1.0~dev<commit date>+<git sha>)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
OUT="${1:-$ROOT/dist}"
: "${PYTHON_DIR:?set PYTHON_DIR to a python-build-standalone installation}"
# Dev versions sort by commit date (Debian ordering), then name the commit.
VERSION="${VERSION:-0.1.0~dev$(git -C "$ROOT" log -1 --format=%cd --date=format:%Y%m%d%H%M)+$(git -C "$ROOT" rev-parse --short HEAD)}"
PREFIX=/opt/carborewind-agent

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

echo "==> bundling Python from $PYTHON_DIR"
mkdir -p "$STAGE$PREFIX"
cp -a "$PYTHON_DIR" "$STAGE$PREFIX/python"
PY="$STAGE$PREFIX/python/bin/python3"
"$PY" -c 'import sys; assert sys.version_info >= (3, 11), sys.version'
# uv marks its interpreters as externally managed; this private copy is ours to manage.
rm -f "$STAGE$PREFIX/python/lib/python3."*/EXTERNALLY-MANAGED

echo "==> installing the agent and its dependencies (binary wheels only)"
"$PY" -m pip install --quiet --no-cache-dir --disable-pip-version-check \
    --only-binary=:all: --no-compile "$ROOT/agent"
# The runtime only needs the agent: drop pip, tests, headers and caches.
"$PY" -m pip uninstall --quiet --yes pip >/dev/null 2>&1 || true
rm -rf "$STAGE$PREFIX/python/include" "$STAGE$PREFIX/python/share" \
       "$STAGE$PREFIX/python/lib/python3."*/test \
       "$STAGE$PREFIX/python/lib/python3."*/idlelib \
       "$STAGE$PREFIX/python/lib/python3."*/tkinter \
       "$STAGE$PREFIX/python/lib/python3."*/ensurepip \
       "$STAGE$PREFIX/python/lib/python3."*/config-* \
       "$STAGE$PREFIX/python/lib/python3."*/lib-dynload/_tkinter* \
       "$STAGE$PREFIX/python/lib/"tcl* "$STAGE$PREFIX/python/lib/"tk* \
       "$STAGE$PREFIX/python/lib/"itcl* "$STAGE$PREFIX/python/lib/"thread* \
       "$STAGE$PREFIX/python/lib/"libtcl* "$STAGE$PREFIX/python/lib/"libtk* \
       "$STAGE$PREFIX/python/lib/pkgconfig"
find "$STAGE$PREFIX/python/lib" -maxdepth 1 -name '*.a' -delete
find "$STAGE$PREFIX" -name '__pycache__' -type d -prune -exec rm -rf {} +
find "$STAGE$PREFIX/python/bin" -type f ! -name 'python3*' -delete
"$PY" -I -m compileall -q -j0 "$STAGE$PREFIX/python/lib" >/dev/null || true
"$PY" -I -m carborewind_agent --version

echo "==> filesystem layout"
install -D -m 0755 "$HERE/carborewind-agent.sh" "$STAGE/usr/bin/carborewind-agent"
install -D -m 0644 "$HERE/carborewind-agent.service" \
    "$STAGE/lib/systemd/system/carborewind-agent.service"
install -D -m 0640 "$HERE/agent.env" "$STAGE/etc/carborewind/agent.env"
install -D -m 0644 "$ROOT/docs/install.md" "$STAGE/usr/share/doc/carborewind-agent/INSTALL.md"
install -D -m 0644 "$ROOT/LICENSE" "$STAGE/usr/share/doc/carborewind-agent/copyright"

mkdir -p "$STAGE/DEBIAN"
SIZE="$(du -sk --exclude=DEBIAN "$STAGE" | cut -f1)"
sed -e "s/@VERSION@/$VERSION/" -e "s/@SIZE@/$SIZE/" "$HERE/debian/control.in" \
    > "$STAGE/DEBIAN/control"
install -m 0755 "$HERE/debian/postinst" "$HERE/debian/prerm" "$HERE/debian/postrm" "$STAGE/DEBIAN/"
echo "/etc/carborewind/agent.env" > "$STAGE/DEBIAN/conffiles"

mkdir -p "$OUT"
DEB="$OUT/carborewind-agent_${VERSION}_amd64.deb"
dpkg-deb --root-owner-group --build -Zxz "$STAGE" "$DEB" >/dev/null
(cd "$OUT" && sha256sum "$(basename "$DEB")" > "$(basename "$DEB").sha256")
echo "==> $DEB"
