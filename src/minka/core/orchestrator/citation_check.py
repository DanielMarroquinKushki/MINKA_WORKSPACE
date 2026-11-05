"""citation_check — verifica las citas `archivo:línea` de la RESPUESTA del agente contra REPOS.

Diseño: docs/diseño-6b-citas.md. Corre en el gancho post-respuesta (`Orchestrator._record_turn`),
sin modelo ni git: reutiliza `RepoIndex` de core/tools/analysis/citations.py. Anota la cita en el
mismo texto (`ruta.go:12 ⚠ no existe`, `ruta.go:900 ⚠ línea fuera de rango`) sin reescribir nada
más; el texto anotado es el que se muestra y el que hereda la memoria, para que el siguiente
turno no repita la cita como cierta.

Cuentan como "no encontradas": inexistente, fuera_rango, fuera_de_repos. `ambigua` y `elidida`
no se marcan. No se verifica lo que va dentro de bloques ``` ni de literales entre comillas.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from minka.core.tools.analysis import citations as cit

MARK_MISSING = "⚠ no existe"
MARK_RANGE = "⚠ línea fuera de rango"

_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_QUOTED_RE = re.compile(r'"[^"\n]*"|\'[^\'\n]*\'')


@dataclass
class CheckResult:
    text: str
    total: int = 0
    missing: int = 0

    @property
    def summary(self) -> str:
        return f"{self.total} citas verificadas, {self.missing} no encontradas"


def _mark_for(status: str) -> str | None:
    if status in (cit.STATUS_MISSING, cit.STATUS_OUTSIDE):
        return MARK_MISSING
    if status == cit.STATUS_OUT_OF_RANGE:
        return MARK_RANGE
    return None


def _status(index: cit.RepoIndex, path: str, line: int | None, cache: dict) -> str:
    key = (path, line)
    if key in cache:
        return cache[key]
    status, matches = index.resolve(path)
    if status == cit.STATUS_OK and line is not None:
        _, real = matches[0]
        try:
            n_lines = len(real.read_text(encoding="utf-8", errors="ignore").splitlines())
        except OSError:
            n_lines = None
        if n_lines is not None and line > n_lines:
            status = cit.STATUS_OUT_OF_RANGE
    cache[key] = status
    return status


def _annotate_line(line: str, index: cit.RepoIndex, cache: dict) -> tuple[str, int, int]:
    # Enmascara literales entre comillas: rutas de ejemplo que no son citas.
    masked = _QUOTED_RE.sub(lambda m: " " * len(m.group(0)), line)
    out: list[str] = []
    pos = 0
    total = missing = 0
    for m in cit.CITATION_RE.finditer(masked):
        path, start = m.group(1), m.group(2)
        total += 1
        status = _status(index, path, int(start) if start else None, cache)
        mark = _mark_for(status)
        if mark == MARK_MISSING:
            suggestion = cache.setdefault(("suggest", path), index.suggest(path))
            if suggestion:
                mark = f"{mark} (¿quisiste decir {suggestion[0]}?)"
        end = m.end()
        # Si la cita va entre backticks, el marcador queda fuera del backtick.
        if end < len(line) and line[end] == "`" and m.start() > 0 and line[m.start() - 1] == "`":
            end += 1
        out.append(line[pos:end])
        if mark:
            missing += 1
            if not line[end:].startswith(f" {mark}"):
                out.append(f" {mark}")
        pos = end
    out.append(line[pos:])
    return "".join(out), total, missing


def verify(text: str, index: cit.RepoIndex | None) -> CheckResult:
    """Texto anotado + contadores. Sin repos válidos o sin citas devuelve el texto intacto."""
    if not text or index is None or index.empty:
        return CheckResult(text or "", 0, 0)
    cache: dict = {}
    lines = text.split("\n")
    in_fence = False
    total = missing = 0
    for i, line in enumerate(lines):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        new, t, m = _annotate_line(line, index, cache)
        lines[i] = new
        total += t
        missing += m
    return CheckResult("\n".join(lines), total, missing)
