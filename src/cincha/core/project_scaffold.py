"""
project_scaffold — crea el esqueleto de un proyecto nuevo (Arnés Global).

Lógica pura y testeable: create_project() no imprime ni pregunta nada; solo
copia el _template, renombra los .example, crea el active-context y devuelve un
resumen. La capa interactiva (preguntas/prints) vive en main.py (--new-project).

Datos en config.DATA_DIR (proyectos y active-context); plantilla en
config.TEMPLATE_DIR — sin rutas hardcodeadas.
"""

import re
import shutil
from pathlib import Path

from cincha.config_loader import config

# Nombre válido: kebab-case (minúsculas, números, guiones internos).
_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

# .example → nombre real (se renombran tras copiar el template).
_EXAMPLE_RENAMES = {
    "config.py.example": "config.py",
    "knowledge/team/session_log.md.example": "knowledge/team/session_log.md",
    "knowledge/team/decisions_log.md.example": "knowledge/team/decisions_log.md",
}

# Plantilla del contexto de corto plazo (fuente única en active-context/).
_ACTIVE_CONTEXT_TEMPLATE = """\
# Contexto Activo — {title}
*Proyecto: {name}*

## Ticket actual
(por definir)

## Alcance
(qué se va a tocar en este ticket)

## Bloqueos
Ninguno activo

## Próximo paso
(la siguiente acción concreta)

## Memoria del proyecto
- Historial de sesiones → `dev-agent/projects/{name}/knowledge/team/session_log.md`
- Decisiones (ADRs) → `dev-agent/projects/{name}/knowledge/team/decisions_log.md`
- Arquitectura / flujos → `dev-agent/projects/{name}/knowledge/extracted/`
"""


class ProjectExistsError(Exception):
    """El proyecto ya existe — no se sobreescribe."""


class InvalidProjectNameError(Exception):
    """El nombre no cumple kebab-case o está reservado."""


def _title(name: str) -> str:
    return name.replace("-", " ").title()


# Plantillas de datos del usuario que viajan en el paquete → destino en DATA_DIR.
_INIT_FILES = {
    "config.example.py": "config_local.py",
    ".env.example": ".env",
}


def init_data_dir() -> dict:
    """Copia config.example.py y .env.example a config.DATA_DIR (config_local.py, .env).
    Nunca sobrescribe. Devuelve {"data_dir", "created": [...], "skipped": [...]}."""
    data_dir = Path(config.DATA_DIR)
    data_dir.mkdir(parents=True, exist_ok=True)
    created, skipped = [], []
    for src_name, dst_name in _INIT_FILES.items():
        dst = data_dir / dst_name
        if dst.exists():
            skipped.append(str(dst))
            continue
        shutil.copyfile(Path(config.BASE_DIR) / src_name, dst)
        created.append(str(dst))
    return {"data_dir": str(data_dir), "created": created, "skipped": skipped}


def create_project(
    name: str,
    core_module: str | None = None,
) -> dict:
    """
    Crea el esqueleto de un proyecto nuevo desde projects/_template.
    Devuelve dict con name, project_dir, active_context, next_steps.
    No imprime ni pregunta nada (capa pura, testeable).
    """
    if not name or not _NAME_RE.match(name):
        raise InvalidProjectNameError(
            f"Nombre inválido: '{name}'. Usa kebab-case (ej: mi-proyecto)."
        )
    if name == "_template":
        raise InvalidProjectNameError("'_template' es un nombre reservado.")

    projects_dir = Path(config.PROJECTS_DIR)
    template_dir = Path(config.TEMPLATE_DIR)
    project_dir = projects_dir / name

    if project_dir.exists():
        raise ProjectExistsError(f"El proyecto '{name}' ya existe: {project_dir}")
    if not template_dir.is_dir():
        raise FileNotFoundError(f"No existe el template: {template_dir}")

    # 1. Copia el template completo.
    shutil.copytree(template_dir, project_dir)

    # 2. Renombra los .example a sus nombres reales.
    for example_rel, real_rel in _EXAMPLE_RENAMES.items():
        example_path = project_dir / example_rel
        if example_path.is_file():
            example_path.rename(project_dir / real_rel)

    # 3. Crea el active-context (fuente única de corto plazo).
    active_context_dir = Path(config.DATA_DIR) / "active-context"
    active_context_dir.mkdir(parents=True, exist_ok=True)
    active_context = active_context_dir / f"{name}.md"
    if not active_context.exists():
        active_context.write_text(
            _ACTIVE_CONTEXT_TEMPLATE.format(name=name, title=_title(name)),
            encoding="utf-8",
        )

    # 4. Escribe CORE_MODULE_NAME si se proporcionó.
    if core_module:
        _set_core_module(project_dir / "config.py", core_module)

    return {
        "name": name,
        "project_dir": str(project_dir),
        "active_context": str(active_context),
        "next_steps": [
            f"Completa los parámetros en {project_dir / 'config.py'} "
            "(CORE_MODULE_NAME, PR_CHECKLIST, AGENT_KNOWLEDGE, AGENT_TOOLS).",
            f"Agrega las rutas de repos en {Path(config.DATA_DIR) / 'config_local.py'} → REPOS.",
            f"Arranca: {config.APP_NAME} --project {name}",
        ],
    }


def _set_core_module(config_path: Path, core_module: str) -> bool:
    """Reemplaza el valor de CORE_MODULE_NAME en el config.py del proyecto."""
    if not config_path.is_file():
        return False
    text = config_path.read_text(encoding="utf-8")
    new_text = re.sub(
        r'CORE_MODULE_NAME\s*=\s*["\'].*?["\']',
        f'CORE_MODULE_NAME = "{core_module}"',
        text,
        count=1,
    )
    if new_text != text:
        config_path.write_text(new_text, encoding="utf-8")
        return True
    return False
