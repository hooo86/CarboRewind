#!/usr/bin/env bash
# Build carborewind-server-<version>.tar.gz from the committed tree (git archive: no local
# secrets, virtualenvs or node_modules can leak into the bundle).
#
# Usage: deploy/packaging/server/build-bundle.sh [output-dir]   (VERSION overridable)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
OUT="${1:-$ROOT/dist}"
VERSION="${VERSION:-0.1.0~dev+$(git -C "$ROOT" rev-parse --short HEAD)}"
NAME="carborewind-server-${VERSION}"
mkdir -p "$OUT"
git -C "$ROOT" diff --quiet HEAD -- backend frontend deploy docs \
    || echo "warning: uncommitted changes are NOT included in the bundle" >&2
git -C "$ROOT" archive --format=tar.gz --prefix="$NAME/" -o "$OUT/$NAME.tar.gz" HEAD \
    backend frontend deploy docs LICENSE README.md CHANGELOG.md
(cd "$OUT" && sha256sum "$NAME.tar.gz" > "$NAME.tar.gz.sha256")
echo "==> $OUT/$NAME.tar.gz"
