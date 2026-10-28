"""
search_tools — búsqueda de texto y símbolos Go dentro de los repos.

Usa ripgrep si está disponible (rápido); si no, cae a búsqueda en Python.
Todas devuelven strings con formato `archivo:línea: contenido`.
"""

import re
import shutil
import subprocess
from pathlib import Path

_HAS_RG = shutil.which("rg") is not None
_MAX_HITS = 300


def _python_grep(pattern: str, root: Path, glob: str) -> list[str]:
    """Fallback de grep en Python cuando no hay ripgrep."""
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        return [f"ERROR: regex inválida: {exc}"]

    hits: list[str] = []
    for f in root.rglob(glob):
        if "vendor" in f.parts or "node_modules" in f.parts or not f.is_file():
            continue
        try:
            for n, line in enumerate(
                f.read_text(encoding="utf-8", errors="replace").splitlines(), 1
            ):
                if rx.search(line):
                    hits.append(f"{f}:{n}: {line.strip()}")
                    if len(hits) >= _MAX_HITS:
                        hits.append("... [TRUNCADO: demasiadas coincidencias]")
                        return hits
        except Exception:  # noqa: BLE001
            continue
    return hits


def grep_code(pattern: str, path: str, glob: str = "*.go") -> str:
    """
    Busca un patrón (regex) en los archivos de `path`.

    Args:
        pattern: expresión regular.
        path: directorio raíz de búsqueda.
        glob: patrón de archivos (default *.go).
    """
    root = Path(path)
    if root.is_file():
        return (
            f"ERROR: {path} es un ARCHIVO, no un directorio. grep_code recorre un "
            "directorio; para leer un archivo usa read_file (acepta start_line y "
            "end_line para acotar el rango)."
        )
    if not root.is_dir():
        return f"ERROR: no existe el directorio {path}"

    if _HAS_RG:
        try:
            proc = subprocess.run(
                [
                    "rg",
                    "--line-number",
                    "--no-heading",
                    "--glob",
                    glob,
                    "--glob",
                    "!vendor",
                    "--glob",
                    "!node_modules",
                    pattern,
                    str(root),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            out = proc.stdout.strip().splitlines()[:_MAX_HITS]
            return "\n".join(out) or "[sin coincidencias]"
        except Exception:  # noqa: BLE001 — si rg falla, cae al fallback
            pass

    hits = _python_grep(pattern, root, glob)
    return "\n".join(hits) or "[sin coincidencias]"


def find_interface(name: str, path: str) -> str:
    """
    Localiza la(s) definición(es) de una interface Go por nombre.
    Detecta el patrón `type <Name> interface`.
    """
    return grep_code(rf"type\s+{re.escape(name)}\s+interface", path)


def find_usages(symbol: str, path: str) -> str:
    """
    Busca todos los usos de un símbolo (función, tipo, constante) en el repo.
    """
    return grep_code(rf"\b{re.escape(symbol)}\b", path)
