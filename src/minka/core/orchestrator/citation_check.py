"""citation_check — verifica las citas `archivo:línea` de la RESPUESTA del agente contra REPOS.

Diseño: docs/diseño-6b-citas.md. Corre en el gancho post-respuesta (`Orchestrator._record_turn`),
sin modelo ni git: reutiliza `RepoIndex` de core/tools/analysis/citations.py. Anota la cita en el
mismo texto (`ruta.go:12 ⚠ no existe`, `ruta.go:900 ⚠ línea fuera de rango`) sin reescribir nada
más; el texto anotado es el que se muestra y el que hereda la memoria, para que el siguiente
turno no repita la cita como cierta.

Cuentan como "no encontradas": inexistente, fuera_rango, fuera_de_repos. `ambigua` y `elidida`
no se marcan. No se verifica lo que va dentro de bloques ``` ni de literales entre comillas.

Símbolo cerca de la cita (paso 12): si la línea trae un identificador en backticks con mayúscula,
`_` o `.`, debe estar a ≤ SYMBOL_WINDOW líneas de la citada (`citations._check_symbols`). Si está
en otra parte del archivo: `⚠ símbolo a N líneas`; si no está: `⚠ símbolo no está en el archivo`.
`total`/`missing` se conservan tal cual; `ok`, `symbol_far` y `symbol_absent` son los nuevos.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from minka.core.tools.analysis import citations as cit

MARK_MISSING = "⚠ no existe"
MARK_RANGE = "⚠ línea fuera de rango"
MARK_SYMBOL_FAR = "⚠ símbolo a {n} líneas"
MARK_SYMBOL_ABSENT = "⚠ símbolo no está en el archivo"

_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_QUOTED_RE = re.compile(r'"[^"\n]*"|\'[^\'\n]*\'')


@dataclass
class CheckResult:
    text: str
    total: int = 0
    missing: int = 0  # inexistente, fuera de rango, fuera de REPOS (contador histórico)
    ok: int = 0  # resuelta, en rango y con el símbolo cerca (o sin símbolo que comprobar)
    symbol_far: int = 0
    symbol_absent: int = 0

    @property
    def summary(self) -> str:
        return (
            f"{self.total} citas verificadas, {self.missing} no encontradas · "
            f"símbolo: ok {self.ok}, lejano {self.symbol_far}, ausente {self.symbol_absent}"
        )


def _mark_for(status: str) -> str | None:
    if status in (cit.STATUS_MISSING, cit.STATUS_OUTSIDE):
        return MARK_MISSING
    if status == cit.STATUS_OUT_OF_RANGE:
        return MARK_RANGE
    return None


def _source_lines(real, cache: dict) -> list[str] | None:
    key = ("lines", real)
    if key not in cache:
        try:
            cache[key] = real.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            cache[key] = None
    return cache[key]


def _status(index: cit.RepoIndex, path: str, line: int | None, cache: dict) -> str:
    key = (path, line)
    if key in cache:
        return cache[key]
    status, matches = index.resolve(path)
    if status == cit.STATUS_OK and line is not None:
        _, real = matches[0]
        lines = _source_lines(real, cache)
        if lines is not None and line > len(lines):
            status = cit.STATUS_OUT_OF_RANGE
    cache[key] = status
    return status


def _symbol_check(index: cit.RepoIndex, path: str, line: int, line_text: str, cache: dict) -> tuple[str, int | None]:
    """(estado, distancia): `ok` si no hay símbolo que comprobar o está a ≤ SYMBOL_WINDOW líneas."""
    symbols = cit.symbols_for_citation(line_text)
    if not symbols:
        return cit.STATUS_OK, None
    _, matches = index.resolve(path)
    lines = _source_lines(matches[0][1], cache)
    if lines is None:
        return cit.STATUS_OK, None
    probe = cit.Citation(doc="respuesta", md_line=0, raw=f"{path}:{line}", path=path, line=line)
    cit._check_symbols(probe, lines, line_text, symbols=symbols)
    return probe.status, probe.distance


def _annotate_line(line: str, index: cit.RepoIndex, cache: dict) -> tuple[str, CheckResult]:
    # Enmascara literales entre comillas: rutas de ejemplo que no son citas.
    masked = _QUOTED_RE.sub(lambda m: " " * len(m.group(0)), line)
    out: list[str] = []
    pos = 0
    counts = CheckResult("")
    for m in cit.CITATION_RE.finditer(masked):
        path, start = m.group(1), m.group(2)
        counts.total += 1
        status = _status(index, path, int(start) if start else None, cache)
        mark = _mark_for(status)
        if mark == MARK_MISSING:
            suggestion = cache.setdefault(("suggest", path), index.suggest(path))
            if suggestion:
                mark = f"{mark} (¿quisiste decir {suggestion[0]}?)"
        if mark:
            counts.missing += 1
        elif status == cit.STATUS_OK and start:
            sym_status, distance = _symbol_check(index, path, int(start), line, cache)
            if sym_status == cit.STATUS_SYMBOL_MOVED:
                counts.symbol_far += 1
                mark = MARK_SYMBOL_FAR.format(n=distance)
            elif sym_status == cit.STATUS_SYMBOL_ABSENT:
                counts.symbol_absent += 1
                mark = MARK_SYMBOL_ABSENT
            else:
                counts.ok += 1
        else:
            counts.ok += 1  # ambigua/elidida o sin número de línea: como hasta ahora, sin marca
        end = m.end()
        # Si la cita va entre backticks, el marcador queda fuera del backtick.
        if end < len(line) and line[end] == "`" and m.start() > 0 and line[m.start() - 1] == "`":
            end += 1
        out.append(line[pos:end])
        if mark and not line[end:].startswith(f" {mark}"):
            out.append(f" {mark}")
        pos = end
    out.append(line[pos:])
    return "".join(out), counts


def verify(text: str, index: cit.RepoIndex | None) -> CheckResult:
    """Texto anotado + contadores. Sin repos válidos o sin citas devuelve el texto intacto."""
    if not text or index is None or index.empty:
        return CheckResult(text or "", 0, 0)
    cache: dict = {}
    lines = text.split("\n")
    in_fence = False
    result = CheckResult("")
    for i, line in enumerate(lines):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        new, counts = _annotate_line(line, index, cache)
        lines[i] = new
        result.total += counts.total
        result.missing += counts.missing
        result.ok += counts.ok
        result.symbol_far += counts.symbol_far
        result.symbol_absent += counts.symbol_absent
    result.text = "\n".join(lines)
    return result
