"""
registry.py — Registro central de tools (fix del bug del prompt original).

El BaseAgent del prompt llamaba a `ToolRegistry.get_tools_for_agent(name)` y
`ToolRegistry.get(name)` sin que existieran. Aquí se implementan de verdad:

- Cada tool se registra con nombre, función, descripción y un JSON Schema de
  input (formato que exige la API de Anthropic para tool-use).
- `get_tools_for_agent(agent)` devuelve la lista de schemas de las tools que ese
  agente puede usar (permite acotar capacidades por rol).
- `get(name)` devuelve la función Python ejecutable.

Diseño simple y explícito: registramos las tools a mano (no auto-introspección)
para tener control total de qué expone cada agente — importante en un sistema
que escribe en repos de pagos.
"""

from pathlib import Path
from typing import Callable

from minka.config_loader import config
from minka.core.tools.analysis.dependency_mapper import check_version_drift, map_dependencies
from minka.core.tools.analysis.go_analyzer import (
    check_core_version,
    detect_patterns,
    parse_interfaces,
)
from minka.core.tools.analysis.risk_analyzer import check_pci_compliance, detect_risks

# --- Importa las funciones tool ------------------------------------------------
from minka.core.tools.filesystem.read_tools import list_files, read_file, scan_directory
from minka.core.tools.filesystem.search_tools import find_interface, find_usages, grep_code
from minka.core.tools.filesystem.write_tools import write_file
from minka.core.tools.git.git_tools import (
    commit_change,
    create_branch,
    current_branch,
    git_log,
    show_diff,
)
from minka.core.tools.git.pr_tools import create_pr_description, validate_diff

# --- Gate de aprobación para tools de escritura --------------------------------
# Las primitivas de write_tools.py son POST-aprobación (las usa el gate una vez
# aprobado). Lo que el agente ve son estos wrappers, que fuerzan el paso por
# ApprovalGate en código — no solo en el prompt.
_gate = None


def _get_gate():
    """Lazy init del ApprovalGate (recursos costosos se crean perezosos)."""
    global _gate
    if _gate is None:
        from minka.core.orchestrator.approval_gate import ApprovalGate

        _gate = ApprovalGate(config)
    return _gate


_REJECTED_MSG = (
    "RECHAZADO: el developer no aprobó este cambio. "
    "NO reintentes la misma escritura; pregúntale qué ajustar."
)


def gated_write_file(path: str, content: str, reason: str = "") -> str:
    """Propone escribir `path` vía ApprovalGate; solo escribe si el dev aprueba."""
    if not _get_gate().request(path, content, reason or "Escritura propuesta por el agente"):
        return _REJECTED_MSG
    return write_file(path, content)


def gated_apply_diff(path: str, old_content: str, new_content: str, reason: str = "") -> str:
    """Reemplazo seguro + gate: valida old_content ANTES de molestar al dev."""
    p = Path(path)
    if not p.is_file():
        return f"ERROR: no existe {path}"
    current = p.read_text(encoding="utf-8", errors="replace")
    if current != old_content:
        return (
            "ERROR: el contenido actual no coincide con old_content. "
            "El archivo cambió desde la última lectura — reintenta la lectura."
        )
    if not _get_gate().request(path, new_content, reason or "Diff propuesto por el agente"):
        return _REJECTED_MSG
    return write_file(path, new_content)


# --- Gate 2ª iteración: operaciones git también pasan por aprobación -----------
# Estos wrappers gatean el camino donde el agente invoca las tools git en su loop
# (create_branch / commit_change): muestran diff/confirmación antes de ejecutar.


def gated_create_branch(repo_path: str, reason: str, prefix: str = "agent/") -> str:
    """Propone crear una rama git; solo la crea si el dev aprueba."""
    if not _get_gate().request(
        file_path=f"[git] rama nueva en {repo_path}",
        new_content=f"git checkout -b {prefix}<slug de: {reason}>\n",
        reason=f"Crear rama: {reason}",
    ):
        return _REJECTED_MSG
    return create_branch(repo_path, reason, prefix)


def gated_commit_change(repo_path: str, message: str, paths: list[str] | None = None) -> str:
    """Propone un commit; solo lo ejecuta si el dev aprueba."""
    files = ", ".join(paths) if paths else "todos los archivos modificados"
    if not _get_gate().request(
        file_path=f"[git] commit en {repo_path}",
        new_content=f"git commit -m '{message}'\n# archivos: {files}\n",
        reason=f"Commit: {message}",
    ):
        return _REJECTED_MSG
    return commit_change(repo_path, message, paths)


class _Tool:
    """Envoltura de una tool: función + metadatos para la API de Anthropic."""

    def __init__(
        self, name: str, fn: Callable, description: str | Callable[[], str], schema: dict
    ):
        self.name = name
        self.fn = fn
        self.description = description
        self.schema = schema

    def to_anthropic(self) -> dict:
        """Formato que espera el parámetro `tools` de la Messages API."""
        return {
            "name": self.name,
            # Callable = se resuelve al construir el schema (sigue a set_project).
            "description": self.description() if callable(self.description) else self.description,
            "input_schema": {
                "type": "object",
                "properties": self.schema.get("properties", {}),
                "required": self.schema.get("required", []),
            },
        }


def _str(desc: str) -> dict:
    return {"type": "string", "description": desc}


# --- Definición de tools (nombre → función + schema) --------------------------
_TOOLS: dict[str, _Tool] = {}


def _register(name, fn, description, properties, required):
    _TOOLS[name] = _Tool(name, fn, description, {"properties": properties, "required": required})


# Filesystem — lectura
_register(
    "read_file",
    read_file,
    "Lee un archivo (opcionalmente un rango de líneas).",
    {
        "path": _str("Ruta absoluta del archivo."),
        "start_line": {"type": "integer", "description": "Primera línea (1-indexed)."},
        "end_line": {"type": "integer", "description": "Última línea; 0 = fin."},
    },
    ["path"],
)
_register(
    "scan_directory",
    scan_directory,
    "Lista la estructura de un directorio filtrando por extensión (excluye vendor/).",
    {
        "path": _str("Directorio raíz."),
        "max_depth": {"type": "integer", "description": "Profundidad máxima."},
        "include_ext": _str("Extensión, ej '.go'. Vacío = todas."),
    },
    ["path"],
)
_register(
    "list_files",
    list_files,
    "Lista rutas que coinciden con un glob recursivo (excluye vendor/).",
    {"path": _str("Directorio raíz."), "pattern": _str("Glob, ej '*.go'.")},
    ["path"],
)

# Filesystem — búsqueda
_register(
    "grep_code",
    grep_code,
    "Busca un patrón regex en archivos (usa ripgrep si está disponible).",
    {
        "pattern": _str("Regex a buscar."),
        "path": _str("Directorio raíz."),
        "glob": _str("Patrón de archivos, ej '*.go'."),
    },
    ["pattern", "path"],
)
_register(
    "find_interface",
    find_interface,
    "Localiza la definición de una interface Go por nombre.",
    {"name": _str("Nombre de la interface."), "path": _str("Directorio raíz.")},
    ["name", "path"],
)
_register(
    "find_usages",
    find_usages,
    "Busca todos los usos de un símbolo en el repo.",
    {"symbol": _str("Símbolo a buscar."), "path": _str("Directorio raíz.")},
    ["symbol", "path"],
)

# Filesystem — escritura (SIEMPRE pasa por ApprovalGate — enforcement en código)
_register(
    "write_file",
    gated_write_file,
    "Propone escribir un archivo. Muestra el diff al dev y pide su aprobación antes de escribir.",
    {
        "path": _str("Ruta absoluta."),
        "content": _str("Contenido completo."),
        "reason": _str("Por qué se propone este cambio (se muestra al dev en el gate)."),
    },
    ["path", "content"],
)
_register(
    "apply_diff",
    gated_apply_diff,
    "Propone un reemplazo seguro: valida old_content, muestra el diff al dev y pide su aprobación.",
    {
        "path": _str("Ruta."),
        "old_content": _str("Contenido esperado actual."),
        "new_content": _str("Contenido nuevo."),
        "reason": _str("Por qué se propone este cambio (se muestra al dev en el gate)."),
    },
    ["path", "old_content", "new_content"],
)

# Git
_register(
    "show_diff",
    show_diff,
    "Muestra el diff de trabajo del repo.",
    {
        "repo_path": _str("Ruta del repo."),
        "staged": {"type": "boolean", "description": "True para diff staged."},
    },
    ["repo_path"],
)
_register(
    "git_log",
    git_log,
    "Últimos N commits del repo.",
    {"repo_path": _str("Ruta del repo."), "n": {"type": "integer", "description": "Cantidad."}},
    ["repo_path"],
)
_register(
    "current_branch",
    current_branch,
    "Rama actual del repo.",
    {"repo_path": _str("Ruta del repo.")},
    ["repo_path"],
)
_register(
    "create_branch",
    gated_create_branch,
    "Propone crear una rama nueva. Muestra la operación al dev y pide su aprobación antes de crearla.",
    {
        "repo_path": _str("Ruta del repo."),
        "reason": _str("Razón; genera el slug."),
        "prefix": _str("Prefijo de rama, ej 'agent/'."),
    },
    ["repo_path", "reason"],
)
_register(
    "commit_change",
    gated_commit_change,
    "Propone stage + commit. Muestra el mensaje al dev y pide su aprobación antes de ejecutar "
    "(SOLO Nivel 5+ o pedido explícito del dev).",
    {
        "repo_path": _str("Ruta del repo."),
        "message": _str("Mensaje de commit."),
        "paths": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Archivos; vacío = todos.",
        },
    },
    ["repo_path", "message"],
)

# PR
_register(
    "create_pr_description",
    create_pr_description,
    "Genera plantilla Markdown de descripción de PR con el diff.",
    {
        "repo_path": _str("Ruta del repo."),
        "title": _str("Título del PR."),
        "summary": _str("Resumen."),
    },
    ["repo_path", "title", "summary"],
)
_register(
    "validate_diff",
    validate_diff,
    "Validación heurística del diff (secretos, panic, TODOs).",
    {"repo_path": _str("Ruta del repo.")},
    ["repo_path"],
)

# Análisis
_register(
    "parse_interfaces",
    parse_interfaces,
    "Lista interfaces Go del repo con archivo:línea.",
    {
        "path": _str("Directorio raíz."),
        "include_mocks": {"type": "boolean", "description": "Incluir mocks/."},
    },
    ["path"],
)
_register(
    "check_core_version",
    check_core_version,
    lambda: (
        f"Versión de {getattr(config, 'CORE_MODULE_NAME', '') or 'módulo core'} "
        "y de Go en el go.mod."
    ),
    {"path": _str("Ruta del repo.")},
    ["path"],
)
_register(
    "detect_patterns",
    detect_patterns,
    "Heurística de patrones de diseño presentes.",
    {"path": _str("Directorio raíz.")},
    ["path"],
)
_register(
    "detect_risks",
    detect_risks,
    "Detecta riesgos de seguridad/PCI con archivo:línea.",
    {"path": _str("Directorio raíz.")},
    ["path"],
)
_register(
    "check_pci_compliance",
    check_pci_compliance,
    "Chequeo PCI-DSS enfocado.",
    {"path": _str("Directorio raíz.")},
    ["path"],
)
_register(
    "map_dependencies",
    map_dependencies,
    "Lista dependencias externas del go.mod.",
    {"path": _str("Ruta del repo.")},
    ["path"],
)
_register(
    "check_version_drift",
    check_version_drift,
    "Compara versiones de core entre repos (recibe dict REPOS).",
    {"repos": {"type": "object", "description": "Dict {alias: ruta} de config.REPOS."}},
    ["repos"],
)


# RAG — Nivel 4 (import perezoso: el arranque no depende de chromadb)
def _semantic_search_tool(
    query: str, n_results: int = 10, repo_filter: str = None, collection: str = "codebase"
) -> list:
    """Wrapper que inyecta config y difiere el import del searcher."""
    from minka.core.tools.rag.searcher import semantic_search

    return semantic_search(
        query, config, n_results=n_results, repo_filter=repo_filter, collection=collection
    )


_register(
    "semantic_search",
    _semantic_search_tool,
    "Búsqueda semántica (RAG). Elige la colección con 'collection': 'codebase' para "
    "código Go (funciones, structs, interfaces) y 'knowledge_docs' para documentación "
    ".md (patrones de diseño, PCI, arquitectura hexagonal, decisiones ADR, flujos). "
    "Recibe una pregunta en lenguaje natural y devuelve los fragmentos más relevantes "
    "con archivo:línea.",
    {
        "query": _str("Pregunta en lenguaje natural, ej 'dónde se valida el KSN'."),
        "n_results": {"type": "integer", "description": "Número de resultados (default 10)."},
        "repo_filter": {
            "type": "string",
            "enum": ["pos", "hsm", "core"],
            "description": "Filtrar por repo (opcional, solo aplica a codebase).",
        },
        "collection": {
            "type": "string",
            "enum": ["codebase", "knowledge_docs"],
            "description": "Colección a consultar (default codebase).",
        },
    },
    ["query"],
)


# Perfiles de tools por agente: viven en config.AGENT_TOOLS (parametrizable
# por proyecto — Arnés Global). Seguridad por diseño: solo development_agent
# accede a escritura/git de commit.


class ToolRegistry:
    """API estática usada por BaseAgent."""

    @staticmethod
    def get_tools_for_agent(agent_name: str) -> list[dict]:
        """Devuelve los schemas Anthropic de las tools permitidas al agente."""
        names = getattr(config, "AGENT_TOOLS", {}).get(agent_name, [])
        return [_TOOLS[n].to_anthropic() for n in names if n in _TOOLS]

    @staticmethod
    def get(name: str) -> Callable:
        """Devuelve la función ejecutable de una tool por nombre."""
        if name not in _TOOLS:
            raise KeyError(f"Tool desconocida: {name}")
        return _TOOLS[name].fn

    @staticmethod
    def all_names() -> list[str]:
        return sorted(_TOOLS.keys())
