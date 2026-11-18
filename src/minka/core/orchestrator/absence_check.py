"""absence_check — cuenta las ausencias que afirma la RESPUESTA y las contrasta con REPOS (paso 12b).

Solo conteo: nunca marca la respuesta ni avisa al modelo. Corre DESPUÉS de mostrar la respuesta
(`main.py`), con tope de `TIME_BUDGET_S` segundos y `MAX_NAMES` nombres distintos por corrida.

Qué cuenta como ausencia dicha: "no existe", "no hay" y "[NO ENCONTRADO" (`branches.ABSENCE_RE`).
Qué se puede comprobar (patrón estricto): "no (existe|hay) (un|una|el|la)? `X`" y "[NO ENCONTRADO: X]".
X se busca con `git grep -q -w -F` solo en el árbol checkouteado de los repos que la misma línea
menciona por alias (o carpeta); si no menciona ninguno, en todos los de REPOS.

Estados por ausencia: `refutada` (X existe en alguno de esos repos), `confirmada` (no existe en
ninguno, todos los grep terminaron) y `no evaluable` (sin backticks, sin repos válidos, fuera del tope
de nombres o de tiempo, o git falló). Contadores del registro: absences_said, absences_refuted,
absences_unclear (confirmadas = dichas − refutadas − no evaluables).
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter

from minka.core.tools.git.branches import ABSENCE_RE

MAX_NAMES = 3
TIME_BUDGET_S = 1.0
NAME_MAX_CHARS = 80

STRICT_RE = re.compile(
    r"\bno (?:existe|hay)(?: (?:un|una|el|la))? `([^`\n]+)`|\[NO ENCONTRADO: ([^\]\n]+)\]",
    re.IGNORECASE,
)
_FENCE_RE = re.compile(r"^\s*(```|~~~)")

STATUS_REFUTED = "refutada"
STATUS_CONFIRMED = "confirmada"
STATUS_UNCLEAR = "no evaluable"


@dataclass
class Absence:
    name: str
    repos: list[str]  # alias que menciona la línea; vacío = todos
    status: str = STATUS_UNCLEAR
    found_in: str | None = None  # alias donde apareció (solo para tests y depuración)


@dataclass
class AbsenceResult:
    said: int = 0
    refuted: int = 0
    unclear: int = 0
    items: list[Absence] = field(default_factory=list)

    @property
    def confirmed(self) -> int:
        return self.said - self.refuted - self.unclear

    @property
    def evaluable(self) -> int:
        return self.said - self.unclear

    def as_fields(self) -> dict[str, int]:
        return {"absences_said": self.said, "absences_refuted": self.refuted, "absences_unclear": self.unclear}

    @property
    def summary(self) -> str:
        return f"ausencias: {self.said} dichas · {self.refuted} refutadas · {self.unclear} no evaluables"


def _prose_lines(text: str) -> list[str]:
    """Líneas fuera de bloques ``` (una ausencia dentro de código no es una afirmación)."""
    out: list[str] = []
    in_fence = False
    for line in (text or "").split("\n"):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence:
            out.append(line)
    return out


def _mentioned_repos(line: str, repos: dict[str, str]) -> list[str]:
    low = line.lower()
    out: list[str] = []
    for alias, path in repos.items():
        names = {alias.lower(), Path(str(path)).name.lower()}
        if any(n and re.search(r"(?<![\w-])" + re.escape(n) + r"(?![\w-])", low) for n in names):
            out.append(alias)
    return out


def extract(text: str, repos: dict[str, str] | None) -> AbsenceResult:
    """Cuenta las ausencias dichas y separa las comprobables (con nombre) de las no evaluables."""
    repos = {k: str(v) for k, v in (repos or {}).items() if v}
    result = AbsenceResult()
    for line in _prose_lines(text):
        said = len(ABSENCE_RE.findall(line))
        if not said:
            continue
        strict = list(STRICT_RE.finditer(line))
        result.said += said
        result.unclear += max(said - len(strict), 0)
        for m in strict:
            name = (m.group(1) or m.group(2) or "").strip()
            if not name or len(name) > NAME_MAX_CHARS:
                result.unclear += 1
                continue
            result.items.append(Absence(name, _mentioned_repos(line, repos)))
    return result


def _git_grep(path: str, name: str, timeout: float) -> bool | None:
    """True si `name` existe (palabra completa, literal) en el árbol checkouteado; False si no;
    None si git falló o se agotó el tiempo."""
    try:
        proc = subprocess.run(
            ["git", "-C", path, "grep", "-q", "-w", "-F", "-e", name, "--"],
            capture_output=True, text=True, timeout=max(timeout, 0.05),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    return None


def check(
    text: str,
    repos: dict[str, str] | None,
    *,
    max_names: int = MAX_NAMES,
    budget_s: float = TIME_BUDGET_S,
    grep=_git_grep,
    clock=perf_counter,
) -> AbsenceResult:
    """Extrae y comprueba. Nunca lanza: cualquier fallo cuenta como no evaluable."""
    repos = {k: str(v) for k, v in (repos or {}).items() if v}
    result = extract(text, repos)
    started = clock()
    checked: dict[tuple[str, tuple[str, ...]], tuple[str, str | None]] = {}
    names_used: set[str] = set()
    for item in result.items:
        targets = item.repos or list(repos)
        key = (item.name, tuple(targets))
        if key in checked:
            item.status, item.found_in = checked[key]
        elif not targets or (item.name not in names_used and len(names_used) >= max_names):
            item.status = STATUS_UNCLEAR
        else:
            names_used.add(item.name)
            status, found = STATUS_CONFIRMED, None
            for alias in targets:
                remaining = budget_s - (clock() - started)
                if remaining <= 0:
                    status = STATUS_UNCLEAR
                    break
                hit = grep(repos[alias], item.name, remaining)
                if hit is None:
                    status = STATUS_UNCLEAR
                    break
                if hit:
                    status, found = STATUS_REFUTED, alias
                    break
            item.status, item.found_in = status, found
            checked[key] = (status, found)
        if item.status == STATUS_REFUTED:
            result.refuted += 1
        elif item.status == STATUS_UNCLEAR:
            result.unclear += 1
    return result
