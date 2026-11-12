#!/usr/bin/env bash
# rollback.sh vANTERIOR
#
# Reinstala la herramienta desde un tag anterior y comprueba `minka --version` y `minka doctor`.
# No toca los datos del usuario (MINKA_HOME, config_local.py, .env, knowledge, métricas):
# solo cambia qué código corre bajo `uv tool`.
#
# Variables: MINKA_RELEASE_URL (origen git; default: el del README), MINKA_EXTRAS (p.ej. "[rag]"),
#            UV_TOOL_DIR / UV_TOOL_BIN_DIR (los respeta; por defecto los de uv).
set -euo pipefail

DIST_NAME="minka-workspace"
APP="minka"

die() { echo "✗ $*" >&2; exit 1; }
ok() { echo "✓ $*"; }
warn() { echo "⚠ $*" >&2; }

TAG="${1:-}"
[[ "$TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "uso: scripts/rollback.sh vX.Y.Z"
VERSION="${TAG#v}"
command -v uv >/dev/null || die "uv no está en PATH"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
URL="${MINKA_RELEASE_URL:-}"
if [[ -z "$URL" && -f "$HERE/README.md" ]]; then
  URL="$(sed -n "s/.*@ \(git+https:\/\/[^@']*\)@v[0-9.]*'.*/\1/p" "$HERE/README.md" | head -1)"
fi
[[ -n "$URL" ]] || die "no sé desde dónde instalar: define MINKA_RELEASE_URL (p.ej. git+https://host/org/repo)"
EXTRAS="${MINKA_EXTRAS:-}"

echo "→ instalando ${DIST_NAME}${EXTRAS} desde ${URL}@${TAG}"
uv tool install --no-progress --force "${DIST_NAME}${EXTRAS} @ ${URL}@${TAG}" >/dev/null 2>&1 \
  || die "uv tool install falló para ${TAG}"
BIN="${UV_TOOL_BIN_DIR:-}"
if [[ -n "$BIN" ]]; then MINKA_BIN="$BIN/$APP"; else MINKA_BIN="$(command -v "$APP" || true)"; fi
[[ -x "${MINKA_BIN:-}" ]] || MINKA_BIN="$(uv tool dir --bin 2>/dev/null)/$APP"
[[ -x "$MINKA_BIN" ]] || die "no encuentro el ejecutable ${APP} tras instalar"

VERSION_LINE="$("$MINKA_BIN" --version)"
echo "  $VERSION_LINE"
echo "$VERSION_LINE" | grep -q "${APP} ${VERSION} " || die "--version no reporta ${VERSION}"
echo "$VERSION_LINE" | grep -q "requested_revision: ${TAG}" || die "--version no reporta requested_revision ${TAG}"
ok "versión ${VERSION} instalada desde ${TAG}"

set +e
"$MINKA_BIN" doctor
DOCTOR_EXIT=$?
set -e
case "$DOCTOR_EXIT" in
  0) ok "doctor: todo en orden" ;;
  1) warn "doctor: hay chequeos en ✗ (son de tu entorno, no de la instalación)" ;;
  *) die "minka doctor no pudo ejecutarse (exit ${DOCTOR_EXIT})" ;;
esac
echo "Rollback a ${TAG} completo. Los datos de MINKA_HOME no se tocaron."
