#!/usr/bin/env bash
# release.sh vX.Y.Z [--dry-run]
#
# Prepara una versión SIN tocar el remoto: verifica el repo, construye, crea el tag anotado
# sobre HEAD, instala desde ese tag en un UV_TOOL_DIR temporal y compara `minka --version`.
# Al final SOLO imprime los comandos de push; nunca los ejecuta.
#
# --dry-run: corre las verificaciones y el build; no crea el tag ni instala. Un tag ya
#            existente solo avisa (sirve para el CI).
# Variables: RELEASE_SKIP_GH=1 salta la consulta del CI con `gh` (lectura).
set -euo pipefail

DIST_NAME="minka-workspace"
APP="minka"

die() { echo "✗ $*" >&2; exit 1; }
ok() { echo "✓ $*"; }
warn() { echo "⚠ $*" >&2; }

TAG="${1:-}"
DRY_RUN=0
[[ "${2:-}" == "--dry-run" || "${1:-}" == "--dry-run" ]] && DRY_RUN=1
[[ "$TAG" == "--dry-run" ]] && TAG="${2:-}"
[[ "$TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "uso: scripts/release.sh vX.Y.Z [--dry-run]"
VERSION="${TAG#v}"

ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || die "no es un repositorio git"
cd "$ROOT"
HEAD_SHA="$(git rev-parse HEAD)"

# 1. git status limpio (incluye archivos sin seguimiento no ignorados)
if [[ -n "$(git status --porcelain)" ]]; then
  git status --short >&2
  die "git status no está limpio: commitea o descarta antes de publicar"
fi
ok "git status limpio"

# 2. pyproject.version == X.Y.Z
PY_VERSION="$(sed -n 's/^version = "\([^"]*\)"/\1/p' pyproject.toml | head -1)"
[[ "$PY_VERSION" == "$VERSION" ]] || die "pyproject.toml declara $PY_VERSION y se pide $VERSION"
ok "pyproject.toml = $VERSION"

# 3. README con @vX.Y.Z
grep -q "@${TAG}['\"]" README.md || die "README.md no referencia @${TAG} en la instalación"
ok "README referencia @${TAG}"

# 4. tag inexistente
if git rev-parse -q --verify "refs/tags/${TAG}" >/dev/null; then
  if [[ $DRY_RUN -eq 1 ]]; then
    warn "el tag ${TAG} ya existe (dry-run: se sigue sin crearlo)"
  else
    die "el tag ${TAG} ya existe; sube la versión o borra el tag local"
  fi
else
  ok "tag ${TAG} libre"
fi

# 5. uv.lock consistente
command -v uv >/dev/null || die "uv no está en PATH"
uv lock --check --no-progress >/dev/null 2>&1 || die "uv.lock no corresponde a pyproject.toml (uv lock --check)"
ok "uv lock --check"

# 6. CI del commit (solo lectura; si no hay gh, avisa y sigue)
if [[ "${RELEASE_SKIP_GH:-}" == "1" ]]; then
  warn "RELEASE_SKIP_GH=1: no se consultó el CI"
elif command -v gh >/dev/null; then
  if RUNS="$(gh run list --commit "$HEAD_SHA" --json status,conclusion --limit 20 2>/dev/null)"; then
    if echo "$RUNS" | grep -q '"conclusion":"failure"'; then
      die "el CI del commit ${HEAD_SHA:0:12} tiene corridas en rojo"
    elif echo "$RUNS" | grep -q '"conclusion":"success"'; then
      ok "CI del commit ${HEAD_SHA:0:12} en verde"
    else
      warn "sin corridas concluidas del CI para ${HEAD_SHA:0:12} (pendiente o sin subir)"
    fi
  else
    warn "gh no pudo consultar el CI (sin sesión o sin remoto); se sigue"
  fi
else
  warn "gh no está instalado: CI no verificado"
fi

# 7. build
rm -rf dist
uv build --no-progress >/dev/null 2>&1 || die "uv build falló"
ok "build: $(ls dist/*.whl | xargs -n1 basename)"

if [[ $DRY_RUN -eq 1 ]]; then
  echo "dry-run: verificaciones y build en orden; sin tag, sin instalación, sin push."
  exit 0
fi

# 8. tag anotado sobre HEAD (local)
git tag -a "$TAG" -m "$TAG" "$HEAD_SHA"
ok "tag ${TAG} creado sobre ${HEAD_SHA:0:12} (solo local)"

# 9. instalación desde el tag en un UV_TOOL_DIR temporal y comparación con --version
TOOL_DIR="$(mktemp -d)"; BIN_DIR="$(mktemp -d)"; HOME_DIR="$(mktemp -d)"
cleanup() { rm -rf "$TOOL_DIR" "$BIN_DIR" "$HOME_DIR"; }
trap cleanup EXIT
UV_TOOL_DIR="$TOOL_DIR" UV_TOOL_BIN_DIR="$BIN_DIR" \
  uv tool install --no-progress --force "${DIST_NAME} @ git+file://${ROOT}@${TAG}" >/dev/null 2>&1 \
  || die "no se pudo instalar desde el tag ${TAG}"
VERSION_LINE="$(MINKA_HOME="$HOME_DIR" "$BIN_DIR/$APP" --version)"
echo "  $VERSION_LINE"
echo "$VERSION_LINE" | grep -q "${APP} ${VERSION} " || die "--version no reporta ${VERSION}"
echo "$VERSION_LINE" | grep -q "commit ${HEAD_SHA:0:12}" || die "--version no reporta el commit ${HEAD_SHA:0:12}"
echo "$VERSION_LINE" | grep -q "requested_revision: ${TAG}" || die "--version no reporta requested_revision ${TAG}"
ok "instalación desde ${TAG}: versión y commit coinciden"

cat <<MSG

Listo. Nada se ha enviado al remoto. Para publicar:

  git push origin ${HEAD_SHA:0:12}:$(git rev-parse --abbrev-ref HEAD)
  git push origin ${TAG}

Para deshacer el tag local: git tag -d ${TAG}
MSG
