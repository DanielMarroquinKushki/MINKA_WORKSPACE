"""
config_loader.py — Carga la configuración efectiva del sistema.

Resuelve el bug del prompt original (`from config.local import *`, que no
funciona porque config.py es un módulo, no un paquete).

Estrategia (Arnés Global — merge en 3 capas):
  1. Toma todas las MAYÚSCULAS de config.py como base (global).
  2. Si hay proyecto activo (parámetro explícito o DEFAULT_PROJECT de
     config_local.py), sus MAYÚSCULAS de projects/<proyecto>/config.py
     sobreescriben las globales.
  3. Si existe config_local.py, sus MAYÚSCULAS sobreescriben todo
     (el dev siempre tiene la última palabra: rutas, backend, nivel).
  4. Expone un único objeto `config` con acceso por atributo.

Uso en cualquier módulo:
    from config_loader import config
    print(config.ANTHROPIC_MODEL)

Cambio de proyecto en runtime (main.py --project X):
    from config_loader import set_project
    set_project("amex-colombia")   # muta `config` in place → todos lo ven
"""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import config as _base

_PROJECTS_DIR = Path(__file__).parent / "projects"


def _uppercase_vars(module) -> dict:
    """Extrae solo las constantes (MAYÚSCULAS) de un módulo."""
    return {
        key: getattr(module, key)
        for key in dir(module)
        if key.isupper() and not key.startswith("_")
    }


def _load_project_vars(project: str) -> dict:
    """Carga las MAYÚSCULAS de projects/<project>/config.py (por ruta, con
    importlib: los proyectos no son paquetes Python)."""
    cfg_file = _PROJECTS_DIR / project / "config.py"
    if not cfg_file.is_file():
        raise FileNotFoundError(f"No existe la config del proyecto '{project}': {cfg_file}")
    spec = importlib.util.spec_from_file_location(f"_project_{project}", cfg_file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return _uppercase_vars(module)


def load_config(project: str | None = None) -> SimpleNamespace:
    values = _uppercase_vars(_base)

    # Overrides locales del dev (opcional, no versionado). Se leen primero
    # para conocer DEFAULT_PROJECT, pero se aplican al final (última palabra).
    local_values: dict = {}
    try:
        import config_local as _local  # noqa: WPS433 (import local intencional)

        local_values = _uppercase_vars(_local)
    except ImportError:
        # Sin config_local.py se usan los defaults. Es válido para --help.
        pass

    # Capa proyecto: explícito > DEFAULT_PROJECT local > ninguno (solo global).
    if project is None:
        project = local_values.get("DEFAULT_PROJECT")
    if project:
        values.update(_load_project_vars(project))

    values.update(local_values)

    # USE_BEDROCK se deriva de LLM_BACKEND DESPUÉS del merge: si config_local
    # fija el backend, deja de depender de variables del shell.
    backend = (values.get("LLM_BACKEND") or "").strip().lower()
    if backend:
        values["USE_BEDROCK"] = backend == "bedrock"

    ns = SimpleNamespace(**values)
    ns.ACTIVE_PROJECT = project  # None = sin proyecto (defaults globales)

    # Adjunta helpers (funciones, no constantes) que los módulos necesitan a
    # través de `config`. _uppercase_vars solo copia MAYÚSCULAS, así que las
    # funciones del módulo base se exponen aquí explícitamente.
    if hasattr(_base, "get_aws_session"):
        ns.get_aws_session = _base.get_aws_session

    return ns


# Objeto de configuración compartido por todo el sistema.
config = load_config()


def set_project(project: str) -> None:
    """Recarga la config con otro proyecto, mutando `config` IN PLACE para
    que todos los módulos que ya hicieron `from config_loader import config`
    vean los valores nuevos (comparten la referencia al namespace)."""
    new = load_config(project)
    config.__dict__.clear()
    config.__dict__.update(new.__dict__)
