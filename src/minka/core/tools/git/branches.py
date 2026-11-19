"""branches — rama checkouteada de cada repo de REPOS y su distancia a la base (solo lectura).

`git rev-parse --abbrev-ref HEAD` y `git rev-list --left-right --count <base>...HEAD`, con base
master → main → origin/master → origin/main. Sin red. El orquestador toma una foto por sesión y la
refresca al inicio de cada corrida; se muestra en `/status` y en el resumen de la corrida (stderr),
NUNCA en el mensaje al modelo (movería la caché). Cuando la respuesta niega algo ("no existe", "no
hay", "[NO ENCONTRADO") y algún repo no está en su base, se añade la línea
"Búsqueda sobre: pos@rama (+N sobre master), hsm@rama (M detrás)".
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from time import perf_counter

BASES = ("master", "main", "origin/master", "origin/main")
GIT_TIMEOUT = 5
# Ausencias en prosa y la marca de citation_check; también la del verificador de knowledge.
ABSENCE_RE = re.compile(r"\bno (?:existe|hay)\b|\[NO ENCONTRADO", re.IGNORECASE)


@dataclass(frozen=True)
class BranchInfo:
    alias: str
    branch: str | None  # None: no es un repo git o no se pudo leer
    base: str | None  # rama base encontrada (master/main/origin/*)
    ahead: int = 0  # commits de HEAD que la base no tiene
    behind: int = 0  # commits de la base que HEAD no tiene

    @property
    def on_base(self) -> bool:
        return self.branch is None or self.base is None or self.branch == self.base.removeprefix("origin/")

    def label(self, base_name: str = "master") -> str:
        if self.branch is None:
            return f"{self.alias}@?"
        parts = []
        if self.ahead:
            parts.append(f"+{self.ahead} sobre {base_name}")
        if self.behind:
            parts.append(f"{self.behind} detrás")
        return f"{self.alias}@{self.branch}" + (f" ({', '.join(parts)})" if parts else "")


def _git(path: str, *args: str) -> str | None:
    try:
        proc = subprocess.run(["git", "-C", path, *args], capture_output=True, text=True, timeout=GIT_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def inspect(alias: str, path: str) -> BranchInfo:
    branch = _git(path, "rev-parse", "--abbrev-ref", "HEAD")
    if branch is None:
        return BranchInfo(alias, None, None)
    base = next((b for b in BASES if _git(path, "rev-parse", "--verify", "-q", b) is not None), None)
    if base is None:
        return BranchInfo(alias, branch, None)
    counts = _git(path, "rev-list", "--left-right", "--count", f"{base}...HEAD") or "0\t0"
    try:
        behind, ahead = (int(x) for x in counts.split())
    except ValueError:
        behind, ahead = 0, 0
    return BranchInfo(alias, branch, base, ahead, behind)


class RepoBranches:
    """Foto de las ramas de REPOS, con el coste de tomarla (`elapsed_ms`)."""

    def __init__(self, repos: dict[str, str] | None):
        started = perf_counter()
        self.infos: dict[str, BranchInfo] = {a: inspect(a, str(p)) for a, p in (repos or {}).items() if p}
        self.elapsed_ms = int((perf_counter() - started) * 1000)

    def off_base(self) -> list[BranchInfo]:
        return [i for i in self.infos.values() if i.branch is not None and not i.on_base]

    def status_line(self) -> str:
        """Para /status: todas las ramas; '—' sin repos."""
        if not self.infos:
            return "—"
        base = next((i.base for i in self.infos.values() if i.base), "master").removeprefix("origin/")
        return " | ".join(i.label(base) for i in self.infos.values()) + f" · {self.elapsed_ms} ms"

    def search_note(self, files: str | None = None, warning: str | None = None) -> str | None:
        """Línea tenue para una respuesta que niega algo. Partes: repos fuera de su base, "archivos: <globs
        recorridos>" y un aviso ("no se buscó en YAML/config"). None si todo está en la base y no hay aviso:
        no se añade ruido a cada negación."""
        off = self.off_base()
        if not off and not warning:
            return None
        parts: list[str] = []
        if off:
            base = (off[0].base or "master").removeprefix("origin/")
            parts.append(", ".join(i.label(base) for i in off))
        if files:
            parts.append(f"archivos: {files}")
        if warning:
            parts.append(warning)
        return "Búsqueda sobre: " + " · ".join(parts)


def mentions_absence(text: str) -> bool:
    return bool(ABSENCE_RE.search(text or ""))
