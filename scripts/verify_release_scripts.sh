#!/usr/bin/env bash
# Prueba release.sh y rollback.sh contra un repositorio git local temporal (file://), nunca
# contra origin. Copia el árbol de trabajo actual (incluidos cambios sin commit) a un clon,
# publica v9.0.0 y v9.0.1 allí y vuelve a v9.0.0. Un push accidental iría a un bare temporal.
set -euo pipefail

die() { echo "✗ $*" >&2; exit 1; }
ok() { echo "✓ $*"; }

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
command -v uv >/dev/null || die "uv no está en PATH"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
REPO="$TMP/repo"; REMOTE="$TMP/remote.git"; HOME_DIR="$TMP/home"; TOOLS="$TMP/tools"; BIN="$TMP/bin"
mkdir -p "$REPO" "$HOME_DIR" "$TOOLS" "$BIN"

# Clon del árbol de trabajo (seguidos + nuevos no ignorados), sin .git del origen.
git -C "$ROOT" ls-files -z --cached --others --exclude-standard | tar --null -T - -cf - -C "$ROOT" | tar -C "$REPO" -xf -
git -C "$REPO" init -q -b main
git -C "$REPO" config user.email "ci@example.com"
git -C "$REPO" config user.name "ci"
git -C "$REPO" config commit.gpgsign false
git -C "$REPO" config tag.gpgsign false
git -C "$REPO" add -A
git -C "$REPO" commit -q -m "estado de trabajo"
git init -q --bare "$REMOTE"
git -C "$REPO" remote add origin "file://$REMOTE"

set_version() {  # X.Y.Z
  sed -i.bak "s/^version = \"[^\"]*\"/version = \"$1\"/" "$REPO/pyproject.toml" && rm -f "$REPO/pyproject.toml.bak"
  sed -i.bak "s/@v[0-9][0-9.]*'/@v$1'/" "$REPO/README.md" && rm -f "$REPO/README.md.bak"
  (cd "$REPO" && uv lock --no-progress >/dev/null 2>&1)
  git -C "$REPO" add -A && git -C "$REPO" commit -q -m "v$1"
}

set_version 9.0.0
git -C "$REPO" tag -a v9.0.0 -m v9.0.0
set_version 9.0.1

echo "=== release.sh v9.0.1 (repo temporal) ==="
OUT="$(cd "$REPO" && RELEASE_SKIP_GH=1 bash scripts/release.sh v9.0.1)"
echo "$OUT"
git -C "$REPO" rev-parse -q --verify refs/tags/v9.0.1 >/dev/null || die "release no creó el tag"
[[ "$(git -C "$REPO" cat-file -t v9.0.1)" == "tag" ]] || die "el tag no es anotado"
echo "$OUT" | grep -q "git push origin v9.0.1" || die "release no imprimió el comando de push"
[[ -z "$(git -C "$REMOTE" tag)" && -z "$(git -C "$REMOTE" branch)" ]] || die "release tocó el remoto"
ok "release: tag local, remoto intacto, comandos de push impresos"

echo "=== release.sh --dry-run con tag existente ==="
(cd "$REPO" && RELEASE_SKIP_GH=1 bash scripts/release.sh v9.0.1 --dry-run) | grep -q "dry-run" || die "dry-run falló"
ok "dry-run con tag existente avisa y sale 0"

echo "=== release.sh con working tree sucio ==="
echo "x" >> "$REPO/README.md"
if (cd "$REPO" && RELEASE_SKIP_GH=1 bash scripts/release.sh v9.0.2 >/dev/null 2>&1); then die "release aceptó un árbol sucio"; fi
git -C "$REPO" checkout -q -- README.md
ok "release rechaza git status sucio"

echo "=== rollback.sh v9.0.0 ==="
touch "$HOME_DIR/.testigo"
MINKA_RELEASE_URL="git+file://$REPO" UV_TOOL_DIR="$TOOLS" UV_TOOL_BIN_DIR="$BIN" MINKA_HOME="$HOME_DIR" \
  bash "$REPO/scripts/rollback.sh" v9.0.0
MINKA_HOME="$HOME_DIR" "$BIN/minka" --version | grep -q "minka 9.0.0 " || die "rollback no dejó 9.0.0"
[[ "$(ls -A "$HOME_DIR")" == ".testigo" ]] || die "rollback tocó MINKA_HOME"
ok "rollback: 9.0.0 instalada, MINKA_HOME intacto"
echo "verificación completa"
