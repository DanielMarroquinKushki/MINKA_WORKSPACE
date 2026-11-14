#!/usr/bin/env bash
# ci_status.sh <sha>
#
# Consulta con `gh` (solo lectura) el ÚLTIMO run del CI de ese commit y lo resume en una línea.
# Códigos de salida, para que release.sh decida:
#   0  el último run concluyó en success
#   1  el último run concluyó en failure, cancelled, timed_out, startup_failure o action_required
#   2  no hay runs para ese commit (no subido o sin workflow): solo aviso
#   3  hay run pero no ha concluido (queued/in_progress) o concluyó en skipped/neutral: solo aviso
#   4  gh no está instalado o no pudo consultar (sin sesión, sin remoto): solo aviso
set -euo pipefail

SHA="${1:-}"
[[ -n "$SHA" ]] || { echo "uso: scripts/ci_status.sh <sha>" >&2; exit 64; }
SHORT="${SHA:0:12}"

command -v gh >/dev/null || { echo "gh no está instalado: CI de ${SHORT} no verificado"; exit 4; }
if ! RUNS="$(gh run list --commit "$SHA" --json status,conclusion --limit 20 2>/dev/null)"; then
  echo "gh no pudo consultar el CI de ${SHORT} (sin sesión o sin remoto)"
  exit 4
fi

# gh devuelve los runs del más reciente al más antiguo; el primero es el que manda. Se tolera JSON
# compacto (tubería) o con sangría (terminal) quitando los espacios: status y conclusion no los llevan.
FIRST="$(printf '%s' "$RUNS" | tr -d '\n\r\t ' | sed -n 's/^\[\({[^}]*}\).*/\1/p')"
if [[ -z "$FIRST" ]]; then
  echo "sin corridas del CI para ${SHORT} (commit no subido o sin workflow)"
  exit 2
fi
field() { printf '%s' "$FIRST" | sed -n "s/.*\"$1\":\"\([^\"]*\)\".*/\1/p"; }
STATUS="$(field status)"
CONCLUSION="$(field conclusion)"

case "${STATUS}/${CONCLUSION}" in
  completed/success)
    echo "CI del commit ${SHORT} en verde"; exit 0 ;;
  completed/failure|completed/cancelled|completed/timed_out|completed/startup_failure|completed/action_required)
    echo "el último run del CI de ${SHORT} terminó en ${CONCLUSION}"; exit 1 ;;
  completed/*)
    echo "el último run del CI de ${SHORT} concluyó en '${CONCLUSION:-?}' (ni éxito ni fallo)"; exit 3 ;;
  *)
    echo "CI de ${SHORT} aún sin concluir (${STATUS:-?}); espera a que termine y vuelve a correr"; exit 3 ;;
esac
