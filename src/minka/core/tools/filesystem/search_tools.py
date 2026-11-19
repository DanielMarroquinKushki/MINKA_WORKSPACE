"""
search_tools — búsqueda de texto y símbolos Go dentro de los repos.

Usa ripgrep si está disponible (rápido); si no, cae a búsqueda en Python.
Todas devuelven strings con formato `archivo:línea: contenido`.
"""

import fnmatch
import os
import re
import shutil
import subprocess
from pathlib import Path

from minka.config_loader import config
from minka.core import security

_HAS_RG = shutil.which("rg") is not None
_MAX_HITS = 300
NO_HITS = "[sin coincidencias]"
EXTRA_HEADER = "[fuera del stack: {n} coincidencias]"


def is_literal(pattern: str) -> bool:
    """True si la regex no tiene metacaracteres: buscar `pattern` literal da lo mismo."""
    return bool(pattern) and re.escape(pattern) == pattern


def _extra_globs(cfg=None) -> tuple[str, ...]:
    return tuple(g for g in (getattr(cfg or config, "EXTRA_SEARCH_GLOBS", ()) or ()) if g)


def _skip_names(cfg=None) -> tuple[str, ...]:
    return tuple(getattr(cfg or config, "EXTRA_SEARCH_SKIP", ()) or ())


def _matches_any(name: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatch(name, pat.rstrip("/")) for pat in patterns)


def extra_search(name: str, root: Path, cfg=None) -> list[str]:
    """Pasada única fuera del stack para un NOMBRE LITERAL: recorre `root` con os.walk podando las carpetas
    de EXTRA_SEARCH_SKIP y las excluidas (security.is_excluded), mira solo archivos que casan con
    EXTRA_SEARCH_GLOBS (y no con EXTRA_SEARCH_SKIP ni EXCLUDE_PATTERNS), de hasta EXTRA_SEARCH_MAX_FILE_BYTES,
    EXTRA_SEARCH_PER_FILE coincidencias por archivo y EXTRA_SEARCH_MAX_HITS en total. Cada línea se trunca a
    EXTRA_SEARCH_LINE_CHARS y pasa por security.redact. [] si está apagada (globs vacíos) o no hay nada."""
    cfg = cfg or config
    globs = _extra_globs(cfg)
    if not name or not globs:
        return []
    skip = _skip_names(cfg)
    max_bytes = int(getattr(cfg, "EXTRA_SEARCH_MAX_FILE_BYTES", 1_000_000))
    per_file = int(getattr(cfg, "EXTRA_SEARCH_PER_FILE", 3))
    max_hits = int(getattr(cfg, "EXTRA_SEARCH_MAX_HITS", 20))
    width = int(getattr(cfg, "EXTRA_SEARCH_LINE_CHARS", 160))
    # Identificadores: palabra completa; otros nombres (rutas, "/logon"): subcadena literal.
    rx = re.compile(rf"\b{re.escape(name)}\b" if re.fullmatch(r"\w+", name) else re.escape(name))
    hits: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames if not _matches_any(d, skip) and not security.is_excluded(Path(dirpath) / d)
        )
        for fname in sorted(filenames):
            if not _matches_any(fname, globs) or _matches_any(fname, skip):
                continue
            f = Path(dirpath) / fname
            if security.is_excluded(f):
                security.note_excluded()
                continue
            try:
                if not f.is_file() or f.stat().st_size > max_bytes:
                    continue
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            found = 0
            for n, line in enumerate(text.splitlines(), 1):
                if not rx.search(line):
                    continue
                shown = line.strip()
                if len(shown) > width:
                    shown = shown[:width] + "…"
                hits.append(f"{f}:{n}: {security.redact(shown)[0]}")
                found += 1
                if found >= per_file or len(hits) >= max_hits:
                    break
            if len(hits) >= max_hits:
                return hits
    return hits


def _with_extra(result: str, pattern: str, root: Path, glob: str, literal_name: str | None) -> str:
    """Si la pasada del stack no dio nada y el patrón es literal, anexa la pasada fuera del stack."""
    if result != NO_HITS:
        return result
    from minka.core import stacks

    if glob != stacks.profile().glob:
        return result  # el agente ya eligió otro glob: se respeta tal cual
    name = literal_name if literal_name is not None else (pattern if is_literal(pattern) else None)
    if not name:
        return result
    extra = extra_search(name, root)
    if not extra:
        return result
    return "\n".join([f"{NO_HITS[:-1]} en {glob}]", EXTRA_HEADER.format(n=len(extra)), *extra])


def _python_grep(pattern: str, root: Path, glob: str) -> list[str]:
    """Fallback de grep en Python cuando no hay ripgrep."""
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        return [f"ERROR: regex inválida: {exc}"]

    hits: list[str] = []
    for f in root.rglob(glob):
        if "vendor" in f.parts or "node_modules" in f.parts or ".git" in f.parts or not f.is_file():
            continue
        if security.is_excluded(f):
            security.note_excluded()
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


def _rg_exclude_globs() -> list[str]:
    """`--glob !patrón` por cada patrón excluido (rg aplica los globs a cada componente)."""
    out: list[str] = []
    for pat in security.exclude_patterns():
        out += ["--glob", f"!{pat.rstrip('/')}"]
    return out


def _drop_excluded_hits(lines: list[str]) -> list[str]:
    """Defensa en profundidad: quita las líneas `ruta:n: …` cuya ruta esté excluida."""
    kept = []
    for line in lines:
        path = line.split(":", 1)[0]
        if path and security.is_excluded(path):
            security.note_excluded()
            continue
        kept.append(line)
    return kept


def grep_code(pattern: str, path: str, glob: str | None = None, *, literal_name: str | None = None) -> str:
    """
    Busca un patrón (regex) en los archivos de `path`.

    Args:
        pattern: expresión regular.
        path: directorio raíz de búsqueda.
        glob: patrón de archivos. None = el del STACK del proyecto ("*.go" por defecto).
        literal_name: (interno, no está en el schema) nombre literal que representa `pattern`
            (find_usages). Si la pasada del stack da 0 y el patrón es literal, se anexa la pasada
            fuera del stack (EXTRA_SEARCH_GLOBS: serverless.yml, *.yml… ) con su cabecera.
    """
    if glob is None:
        from minka.core import stacks

        glob = stacks.profile().glob
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
                    *_rg_exclude_globs(),
                    pattern,
                    str(root),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            out = _drop_excluded_hits(proc.stdout.strip().splitlines())[:_MAX_HITS]
            return _with_extra("\n".join(out) or NO_HITS, pattern, root, glob, literal_name)
        except Exception:  # noqa: BLE001 — si rg falla, cae al fallback
            pass

    hits = _python_grep(pattern, root, glob)
    return _with_extra("\n".join(hits) or NO_HITS, pattern, root, glob, literal_name)


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
    return grep_code(rf"\b{re.escape(symbol)}\b", path, literal_name=symbol)
