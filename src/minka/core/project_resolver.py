"""project_resolver — qué proyecto está activo: --project > cwd > DEFAULT_PROJECT.

Inferencia por cwd: recorre projects/*/config.py (sin _template), resuelve los REPOS de cada
proyecto igual que config_loader (REPOS_ROOT, REPOS_OVERRIDES, symlinks con Path.resolve()) y
elige el proyecto cuyo repo contiene el cwd; gana la coincidencia más larga. Un config.py roto
de otro proyecto no rompe nada: se omite y queda como aviso (se muestra con --debug).

Empates (dos proyectos comparten el repo donde estás): DEFAULT_PROJECT si es candidato; si no,
quien llama pregunta en TTY o falla con la lista (exit 2). Nunca se elige en silencio.
MINKA_SOURCE=bridge exige --project explícito.

El resultado por cwd se cachea por proceso (cwd + projects_dir).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from minka import config_loader
from minka._app import SOURCE_ENV

ORIGIN_FLAG = "--project"
ORIGIN_CWD = "cwd"
ORIGIN_DEFAULT = "DEFAULT_PROJECT"

_cache: dict[tuple[str, str], tuple[list[tuple[str, int]], list[str]]] = {}


@dataclass
class Decision:
    name: str | None = None
    origin: str | None = None
    candidates: list[str] = field(default_factory=list)  # >1 = empate sin resolver
    notices: list[str] = field(default_factory=list)  # configs rotos u otros avisos
    error: str | None = None  # mensaje para el usuario; exit 2

    @property
    def ambiguous(self) -> bool:
        return self.name is None and len(self.candidates) > 1

    def label(self) -> str:
        return f"Proyecto activo: {self.name} (por {self.origin})"


def candidates_for_cwd(
    cwd: Path, projects_dir: Path, local_values: dict
) -> tuple[list[tuple[str, int]], list[str]]:
    """[(proyecto, longitud_de_la_coincidencia)] ordenados de mayor a menor, y avisos."""
    key = (str(cwd), str(projects_dir))
    if key in _cache:
        return _cache[key]
    try:
        real_cwd = Path(cwd).resolve()
    except OSError:
        real_cwd = Path(cwd)
    found: list[tuple[str, int]] = []
    notices: list[str] = []
    if projects_dir.is_dir():
        for cfg_file in sorted(projects_dir.glob("*/config.py")):
            name = cfg_file.parent.name
            if name == "_template":
                continue
            try:
                values = config_loader._load_project_vars(name, projects_dir)
            except Exception as exc:  # noqa: BLE001 — un proyecto roto no bloquea a los demás
                notices.append(f"{name}: config.py no se pudo cargar ({type(exc).__name__}); omitido")
                continue
            repos, _ = config_loader.resolve_repos(values, local_values)
            best = 0
            for path in repos.values():
                if not path:
                    continue
                try:
                    root = Path(path).expanduser().resolve()
                except OSError:
                    continue
                if real_cwd == root or root in real_cwd.parents:
                    best = max(best, len(str(root)))
            if best:
                found.append((name, best))
    found.sort(key=lambda item: (-item[1], item[0]))
    _cache[key] = (found, notices)
    return _cache[key]


def reset_cache() -> None:
    _cache.clear()


def decide(
    explicit: str | None,
    cwd: Path | None = None,
    *,
    projects_dir: Path | None = None,
    local_values: dict | None = None,
    bridge: bool | None = None,
) -> Decision:
    """Decisión sin efectos: no carga el proyecto ni imprime. Quien llama hace set_project."""
    if bridge is None:
        bridge = os.getenv(SOURCE_ENV) == "bridge"
    if explicit:
        return Decision(name=explicit, origin=ORIGIN_FLAG)
    if bridge:
        return Decision(error="MINKA_SOURCE=bridge exige --project <nombre>: el bridge no infiere el proyecto.")

    local_values = local_values if local_values is not None else config_loader._load_local_vars(
        config_loader._base.DATA_DIR
    )
    projects_dir = Path(
        projects_dir or local_values.get("PROJECTS_DIR", config_loader._base.PROJECTS_DIR)
    )
    default = local_values.get("DEFAULT_PROJECT")
    found, notices = candidates_for_cwd(cwd or Path.cwd(), projects_dir, local_values)

    if found:
        top = found[0][1]
        tied = [name for name, score in found if score == top]
        if len(tied) == 1:
            return Decision(name=tied[0], origin=ORIGIN_CWD, notices=notices)
        if default in tied:
            return Decision(name=default, origin=ORIGIN_DEFAULT, candidates=tied, notices=notices)
        return Decision(candidates=tied, notices=notices)

    if default:
        return Decision(name=default, origin=ORIGIN_DEFAULT, notices=notices)
    return Decision(
        notices=notices,
        error=(
            "No hay proyecto activo: no estás dentro de un repo de ningún proyecto y config_local.py "
            "no define DEFAULT_PROJECT. Usa `--project <nombre>` o crea uno con `--new-project`."
        ),
    )
