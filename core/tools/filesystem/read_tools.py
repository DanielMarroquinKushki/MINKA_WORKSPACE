"""
read_tools — lectura de archivos y exploración de directorios de los repos Go.

Todas las tools son funciones puras que devuelven strings (lo que espera el
tool_result de la API de Anthropic). No mutan estado.
"""

from pathlib import Path

# Límite de seguridad para no volcar archivos enormes al contexto.
_MAX_CHARS = 200_000


def read_file(path: str, start_line: int = 1, end_line: int = 0) -> str:
    """
    Lee un archivo. Opcionalmente un rango de líneas [start_line, end_line].

    Args:
        path: ruta absoluta del archivo.
        start_line: primera línea (1-indexed). Default 1.
        end_line: última línea inclusive. 0 = hasta el final.
    """
    p = Path(path)
    if p.is_dir():
        return (
            f"ERROR: {path} es un DIRECTORIO, no un archivo. read_file lee un "
            "archivo; para explorar un directorio usa scan_directory o list_files."
        )
    if not p.is_file():
        return f"ERROR: no existe el archivo {path}"

    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        return f"ERROR leyendo {path}: {exc}"

    lines = text.splitlines()
    end = len(lines) if end_line == 0 else min(end_line, len(lines))
    start = max(start_line, 1)
    selected = lines[start - 1 : end]

    numbered = "\n".join(f"{i}\t{line}" for i, line in enumerate(selected, start=start))
    if len(numbered) > _MAX_CHARS:
        numbered = numbered[:_MAX_CHARS] + "\n... [TRUNCADO: archivo demasiado largo]"
    return numbered or "[archivo vacío o rango sin líneas]"


def scan_directory(path: str, max_depth: int = 2, include_ext: str = ".go") -> str:
    """
    Lista la estructura de un directorio hasta `max_depth`, filtrando por
    extensión. Excluye vendor/ y node_modules/ (ruido para el análisis).

    Args:
        path: directorio raíz.
        max_depth: profundidad máxima de recursión.
        include_ext: extensión a incluir (ej. ".go"). "" = todos.
    """
    root = Path(path)
    if root.is_file():
        return (
            f"ERROR: {path} es un ARCHIVO, no un directorio. scan_directory recorre un "
            "directorio; para leer un archivo usa read_file (acepta start_line y "
            "end_line para acotar el rango)."
        )
    if not root.is_dir():
        return f"ERROR: no existe el directorio {path}"

    skip = {"vendor", "node_modules", ".git", ".idea", "__pycache__"}
    out: list[str] = []

    def walk(d: Path, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            entries = sorted(d.iterdir(), key=lambda x: (x.is_file(), x.name))
        except PermissionError:
            return
        for entry in entries:
            if entry.name in skip:
                continue
            rel = entry.relative_to(root)
            if entry.is_dir():
                out.append(f"{'  ' * depth}{rel}/")
                walk(entry, depth + 1)
            elif not include_ext or entry.suffix == include_ext:
                out.append(f"{'  ' * depth}{rel}")

    walk(root, 0)
    result = "\n".join(out)
    if len(result) > _MAX_CHARS:
        result = result[:_MAX_CHARS] + "\n... [TRUNCADO]"
    return result or "[sin archivos que coincidan]"


def list_files(path: str, pattern: str = "*.go") -> str:
    """
    Lista todas las rutas que coinciden con `pattern` (glob recursivo),
    excluyendo vendor/. Útil para catalogar archivos por tipo.
    """
    root = Path(path)
    if root.is_file():
        return (
            f"ERROR: {path} es un ARCHIVO, no un directorio. list_files recorre un "
            "directorio; para leer un archivo usa read_file (acepta start_line y "
            "end_line para acotar el rango)."
        )
    if not root.is_dir():
        return f"ERROR: no existe el directorio {path}"

    matches = [
        str(p)
        for p in root.rglob(pattern)
        if "vendor" not in p.parts and "node_modules" not in p.parts
    ]
    return "\n".join(sorted(matches)) or "[sin coincidencias]"
