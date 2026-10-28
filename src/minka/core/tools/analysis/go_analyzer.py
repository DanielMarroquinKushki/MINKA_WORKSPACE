"""
go_analyzer — análisis estático ligero de código Go (sin compilar).

No pretende reemplazar a go/analysis ni al compilador: extrae señales
estructurales (interfaces, funcs, versión del core) por regex, suficientes
para que los agentes razonen sobre el codebase.
"""

import re
from pathlib import Path

from minka.config_loader import config

_SKIP = {"vendor", "node_modules", ".git"}


def _go_files(root: Path):
    for f in root.rglob("*.go"):
        if not any(part in _SKIP for part in f.parts) and f.is_file():
            yield f


def parse_interfaces(path: str, include_mocks: bool = False) -> str:
    """
    Lista las interfaces Go (`type X interface`) del repo con archivo:línea.
    Por defecto excluye mocks/ (ruido generado por mockery).
    """
    root = Path(path)
    if not root.is_dir():
        return f"ERROR: no existe {path}"

    rx = re.compile(r"type\s+(\w+)\s+interface\b")
    found: list[str] = []
    for f in _go_files(root):
        if not include_mocks and "mocks" in f.parts:
            continue
        for n, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            m = rx.search(line)
            if m:
                found.append(f"{m.group(1)}\t{f}:{n}")
    return "\n".join(sorted(found)) or "[sin interfaces]"


def check_core_version(path: str) -> str:
    """
    Extrae la versión del módulo core compartido (config.CORE_MODULE_NAME)
    y la versión de Go del go.mod del repo.
    Clave para detectar version drift entre servicios.
    """
    core_module = getattr(config, "CORE_MODULE_NAME", "")
    gomod = Path(path) / "go.mod"
    if not gomod.is_file():
        return f"ERROR: no hay go.mod en {path}"

    go_ver = core_ver = "[NO ENCONTRADO EN CÓDIGO]"
    for line in gomod.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if s.startswith("go "):
            go_ver = s[3:].strip()
        # Ignora la línea `module ...` (el propio repo core lleva ese nombre).
        if core_module and core_module in s and not s.startswith("module"):
            parts = s.split()
            if len(parts) >= 2 and parts[-1].startswith("v"):
                core_ver = parts[-1]
            else:
                core_ver = "[es el propio módulo core]"
    return f"go={go_ver} {core_module}={core_ver}"


def detect_patterns(path: str) -> str:
    """
    Heurística de detección de patrones de diseño frecuentes en los repos.
    Devuelve conteos de señales, NO afirma con certeza. El agente confirma.
    """
    root = Path(path)
    if not root.is_dir():
        return f"ERROR: no existe {path}"

    signals = {
        "Factory (New*/Initialize*)": r"func\s+(New|Initialize)\w+",
        "Interface (ports)": r"type\s+\w+\s+interface",
        "Gateway": r"Gateway\b",
        "Middleware": r"[Mm]iddleware",
        "Command": r"[Cc]ommand\b",
        "Strategy (translate/map)": r"[Tt]ranslat|[Mm]apper",
    }
    counts: dict[str, int] = {k: 0 for k in signals}
    compiled = {k: re.compile(v) for k, v in signals.items()}

    for f in _go_files(root):
        if "mocks" in f.parts:
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        for name, rx in compiled.items():
            counts[name] += len(rx.findall(text))

    return "\n".join(f"{k}: {v} señales" for k, v in counts.items())
