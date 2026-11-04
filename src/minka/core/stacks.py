"""stacks — perfil del lenguaje del proyecto (STACK) y cumplimiento opcional (COMPLIANCE).

`projects/<p>/config.py` puede declarar:
    STACK = "go" | "python" | "node"      # ausente = "go" (proyectos existentes no cambian)
    COMPLIANCE = ["pci"]                  # ausente = esquema viejo: ["pci"] solo si STACK también
                                          # está ausente; con STACK declarado, ausente = []

El perfil fija lo que hoy estaba cableado a Go: extensiones de código que recorren
scan_directory, list_files, grep_code, detect_risks y el indexer RAG; el comando de tests;
el archivo de dependencias; el knowledge profesional por defecto y los patrones de bloque
para trocear archivos. Las tools exclusivas de Go (GO_ONLY_TOOLS) solo se registran con
STACK = go; `check_pci_compliance` y la checklist PCI solo con "pci" en COMPLIANCE.

Regla de oro: con STACK y COMPLIANCE ausentes, todo (system, knowledge, schemas de tools,
reglas de riesgo) debe quedar byte a byte como antes de este módulo.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from minka.config_loader import config

DEFAULT_STACK = "go"
_SENTINEL = object()

GO_ONLY_TOOLS = frozenset(
    {
        "find_interface",  # `type X interface`
        "parse_interfaces",
        "detect_patterns",
        "check_core_version",  # go.mod
        "map_dependencies",  # go.mod
        "check_version_drift",  # go.mod
    }
)
PCI_TOOLS = frozenset({"check_pci_compliance"})

# Solo lo que nunca es código del proyecto; lo específico (vendor, .venv, dist…) va en el perfil,
# así un repo Go recorre exactamente lo mismo que antes (todo salvo vendor/).
_COMMON_SKIP = (".git", "node_modules")

_GENERIC_KNOWLEDGE = {
    "architecture_agent": ["professional/hexagonal_architecture.md", "professional/design_patterns.md"],
    "development_agent": ["professional/code_review_checklist.md", "team/team_pillars.md"],
    "pr_review_agent": ["professional/code_review_checklist.md", "team/team_pillars.md"],
    "debug_agent": [],
    "report_agent": [],
    "meta_agent": [],
}


@dataclass(frozen=True)
class StackProfile:
    name: str
    extensions: tuple[str, ...]  # (".go",)
    test_cmd: tuple[str, ...]
    deps_file: str
    skip_dirs: tuple[str, ...]
    best_practices_doc: str | None  # knowledge profesional propio del stack
    block_patterns: tuple[tuple[str, str], ...]  # (regex, tipo) para trocear archivos

    @property
    def ext(self) -> str:
        return self.extensions[0]

    @property
    def glob(self) -> str:
        return f"*{self.ext}"

    def matches(self, path: Path) -> bool:
        return path.suffix in self.extensions


PROFILES: dict[str, StackProfile] = {
    "go": StackProfile(
        name="go",
        extensions=(".go",),
        test_cmd=("go", "test", "./...", "-v"),
        deps_file="go.mod",
        skip_dirs=("vendor",),
        best_practices_doc="professional/go_best_practices.md",
        block_patterns=(
            (r"^func\s+(\w+)", "func"),
            (r"^func\s+\(.*?\)\s+(\w+)", "method"),
            (r"^type\s+(\w+)\s+interface", "interface"),
            (r"^type\s+(\w+)\s+struct", "struct"),
            (r"^type\s+(\w+)\s+", "type"),
        ),
    ),
    "python": StackProfile(
        name="python",
        extensions=(".py",),
        test_cmd=("python", "-m", "pytest", "-q"),
        deps_file="pyproject.toml",
        skip_dirs=(".venv", "venv", "__pycache__", ".tox", ".mypy_cache", ".ruff_cache"),
        best_practices_doc=None,
        block_patterns=(
            (r"^(?:async\s+)?def\s+(\w+)", "function"),
            (r"^class\s+(\w+)", "class"),
        ),
    ),
    "node": StackProfile(
        name="node",
        extensions=(".ts", ".js", ".tsx", ".jsx"),
        test_cmd=("npm", "test", "--silent"),
        deps_file="package.json",
        skip_dirs=("node_modules", "dist", "build", ".next", "coverage"),
        best_practices_doc=None,
        block_patterns=(
            (r"^(?:export\s+)?(?:async\s+)?function\s+(\w+)", "function"),
            (r"^(?:export\s+)?class\s+(\w+)", "class"),
            (r"^(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\(", "arrow"),
        ),
    ),
}


def _cfg(cfg):
    return cfg if cfg is not None else config


def stack_declared(cfg=None) -> bool:
    """True si el proyecto declara STACK (aunque sea "go")."""
    return getattr(_cfg(cfg), "STACK", None) not in (None, "")


def stack_name(cfg=None) -> str:
    raw = (getattr(_cfg(cfg), "STACK", None) or DEFAULT_STACK).strip().lower()
    return raw if raw in PROFILES else DEFAULT_STACK


def profile(cfg=None) -> StackProfile:
    return PROFILES[stack_name(cfg)]


def compliance(cfg=None) -> frozenset[str]:
    """COMPLIANCE declarado; ausente = {"pci"} solo en proyectos sin STACK (esquema viejo)."""
    cfg = _cfg(cfg)
    declared = getattr(cfg, "COMPLIANCE", None)
    if declared is not None:
        return frozenset(str(c).strip().lower() for c in declared)
    return frozenset({"pci"}) if not stack_declared(cfg) else frozenset()


def tool_allowed(name: str, cfg=None) -> bool:
    if name in GO_ONLY_TOOLS and stack_name(cfg) != "go":
        return False
    if name in PCI_TOOLS and "pci" not in compliance(cfg):
        return False
    return True


def default_knowledge(agent_name: str, cfg=None) -> list[str]:
    """Knowledge profesional por defecto para un agente según STACK y COMPLIANCE."""
    p = profile(cfg)
    docs = list(_GENERIC_KNOWLEDGE.get(agent_name, []))
    if p.best_practices_doc and agent_name in ("development_agent", "pr_review_agent"):
        docs.insert(0, p.best_practices_doc)
    if "pci" in compliance(cfg) and agent_name in ("development_agent", "pr_review_agent"):
        docs.append("professional/pci_checklist.md")
    return docs


def localize(text: str, cfg=None) -> str:
    """Sustituye los ejemplos '.go' / '*.go' de un texto por los del stack activo.
    Con STACK = go devuelve el texto intacto (byte a byte)."""
    p = profile(cfg)
    if p.name == "go":
        return text
    return text.replace("*.go", p.glob).replace(".go", p.ext)


def code_files(root: Path, cfg=None, extra_skip: tuple[str, ...] = ()) -> Iterator[Path]:
    """Archivos de código del stack bajo `root`, saltando .git/node_modules, las carpetas
    propias del stack (vendor, .venv, dist…) y `extra_skip` del llamador."""
    p = profile(cfg)
    skip = set(_COMMON_SKIP) | set(p.skip_dirs) | set(extra_skip)
    for ext in p.extensions:
        for f in sorted(root.rglob(f"*{ext}")):
            if f.is_file() and not any(part in skip for part in f.parts):
                yield f
