"""citations — verifica las citas `archivo:línea` del knowledge contra los repos reales.

Problema: los .md de knowledge/extracted/ (y el catálogo de riesgos) citan código con
`ruta/archivo.go:123`. Los repos cambian y las citas quedan apuntando a archivos
borrados, líneas que ya no existen o símbolos que se movieron. Un agente que hereda
esas citas las repite con seguridad.

Chequeo determinista, sin modelo y sin git: solo filesystem.

Resolución de una cita (en este orden, gana la primera que exista):
  1. ruta literal bajo cada repo de config.REPOS;
  2. alias: el primer segmento coincide (sin distinguir mayúsculas) con una clave de
     REPOS, p. ej. `CORE/middleware/x.go` → REPOS["core"]/middleware/x.go;
  3. el primer segmento es el nombre del directorio del repo (`svc-demo/…`);
  4. por basename: archivos del índice cuya ruta relativa termina en la cita
     (único → resuelta; varios → ambigua).
Toda ruta resuelta se normaliza con resolve() y debe quedar dentro de un repo; lo que
escape (`../`, symlinks hacia fuera) se rechaza.

Estados:
  rotas (exit 1):  inexistente, fuera_rango, fuera_de_repos
  avisos:          ambigua, elidida (`.../`), simbolo_movido, simbolo_ausente
  ok:              ok
"""

from __future__ import annotations

import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

CITED_EXTENSIONS = ("go", "yml", "yaml", "py", "ts", "sql", "java", "kt")
SKIP_DIRS = {".git", "vendor", "node_modules", "mocks", "__pycache__"}
RISK_CATALOG_NAME = "risk_catalog.md"
SYMBOL_WINDOW = 5  # líneas alrededor de la citada donde el símbolo cuenta como "cerca"

STATUS_OK = "ok"
STATUS_MISSING = "inexistente"
STATUS_OUT_OF_RANGE = "fuera_rango"
STATUS_OUTSIDE = "fuera_de_repos"
STATUS_AMBIGUOUS = "ambigua"
STATUS_ELIDED = "elidida"
STATUS_SYMBOL_MOVED = "simbolo_movido"
STATUS_SYMBOL_ABSENT = "simbolo_ausente"

BROKEN_STATUSES = (STATUS_MISSING, STATUS_OUT_OF_RANGE, STATUS_OUTSIDE)
WARNING_STATUSES = (STATUS_AMBIGUOUS, STATUS_ELIDED, STATUS_SYMBOL_MOVED, STATUS_SYMBOL_ABSENT)

_EXT = "|".join(CITED_EXTENSIONS)
CITATION_RE = re.compile(rf"((?:[\w.-]+/)*[\w.-]+\.(?:{_EXT}))\b(?::(\d+)(?:-(\d+))?)?")
RISK_ID_RE = re.compile(r"\bR-\d{2,3}\b")
# Identificadores en backticks de la misma línea: `Nombre`, `Nombre()`, `pkg.Nombre`.
BACKTICK_SYMBOL_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?)(?:\(\))?`")


@dataclass
class Citation:
    doc: str
    md_line: int
    raw: str
    path: str
    line: int | None = None
    line_end: int | None = None
    risk_id: str = ""
    status: str = STATUS_OK
    resolved: str = ""  # ruta relativa al repo que la contiene, como "repo/ruta"
    candidates: list[str] = field(default_factory=list)
    note: str = ""

    @property
    def broken(self) -> bool:
        return self.status in BROKEN_STATUSES

    @property
    def warning(self) -> bool:
        return self.status in WARNING_STATUSES


# ---------------------------------------------------------------------------
# repos
# ---------------------------------------------------------------------------


def _name_tokens(stem: str) -> set[str]:
    """Tokens de 4+ letras de un nombre camelCase/snake_case, en minúsculas."""
    parts = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", stem).replace("_", " ").replace("-", " ").lower().split()
    return {p for p in parts if len(p) >= 4}


def _flat(stem: str) -> str:
    return re.sub(r"[^a-z0-9]", "", stem.lower())


class RepoIndex:
    """Repos de config.REPOS ya resueltos (symlinks incluidos) y su índice por basename."""

    def __init__(self, repos: dict[str, str] | None):
        self.roots: dict[str, Path] = {}
        for key, value in (repos or {}).items():
            if not value:
                continue
            root = Path(value).expanduser()
            if root.is_dir():
                self.roots[key] = root.resolve()
        self._by_basename: dict[str, list[tuple[str, Path]]] | None = None

    @property
    def empty(self) -> bool:
        return not self.roots

    def contains(self, path: Path) -> str | None:
        """Clave del repo que contiene `path` (ya resuelto), o None si queda fuera."""
        for key, root in self.roots.items():
            if path == root or root in path.parents:
                return key
        return None

    def relative(self, key: str, path: Path) -> str:
        return f"{self.roots[key].name}/{path.relative_to(self.roots[key]).as_posix()}"

    def _index(self) -> dict[str, list[tuple[str, Path]]]:
        if self._by_basename is None:
            index: dict[str, list[tuple[str, Path]]] = defaultdict(list)
            for key, root in self.roots.items():
                for dirpath, dirnames, filenames in os.walk(root):
                    dirnames[:] = sorted(
                        d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")
                    )
                    for name in filenames:
                        if name.endswith(tuple(f".{e}" for e in CITED_EXTENSIONS)):
                            index[name].append((key, Path(dirpath) / name))
            self._by_basename = index
        return self._by_basename

    def _accept(self, key: str, candidate: Path) -> tuple[str, Path] | None:
        """Acepta `candidate` solo si existe y, resuelto, sigue dentro de algún repo."""
        if not candidate.is_file():
            return None
        real = candidate.resolve()
        owner = self.contains(real)
        if owner is None:
            return None
        return owner, real

    def suggest(self, cited: str, limit: int = 1) -> list[str]:
        """Archivos de los repos con basename parecido al citado (para "¿quisiste decir…?").
        Compara el nombre sin extensión normalizado (camelCase/_ → tokens) por similitud y
        por tokens de 4+ letras en común; devuelve rutas `repo/…` ordenadas por parecido."""
        import difflib

        base = cited.split("/")[-1]
        stem = base.rsplit(".", 1)[0]
        if not stem:
            return []
        want_ext = base.rsplit(".", 1)[-1] if "." in base else None
        want_tokens = _name_tokens(stem)
        scored: list[tuple[float, str]] = []
        for name, entries in self._index().items():
            if want_ext and not name.endswith("." + want_ext):
                continue
            other = name.rsplit(".", 1)[0]
            ratio = difflib.SequenceMatcher(None, _flat(stem), _flat(other)).ratio()
            shared = {a for a in want_tokens for b in _name_tokens(other) if a[:5] == b[:5]}
            score = max(ratio, 0.5 + 0.25 * len(shared)) if shared else ratio
            if score < 0.6:
                continue
            for key, path in entries:
                scored.append((score, self.relative(key, path.resolve()) if self.contains(path.resolve()) else path.as_posix()))
        scored.sort(key=lambda t: (-t[0], t[1]))
        return [p for _, p in scored[:limit]]

    def resolve(self, cited: str) -> tuple[str, list[tuple[str, Path]]]:
        """Devuelve (status, matches) con matches = [(repo_key, ruta_real)]."""
        parts = cited.split("/")
        if "..." in parts or "…" in parts:
            return STATUS_ELIDED, []
        if ".." in parts:
            return STATUS_OUTSIDE, []

        # 1. literal bajo cada repo
        literal = [m for m in (self._accept(k, r / cited) for k, r in self.roots.items()) if m]
        if literal:
            return (STATUS_OK if len(literal) == 1 else STATUS_AMBIGUOUS), literal

        if len(parts) > 1:
            head, rest = parts[0], "/".join(parts[1:])
            # 2. alias = clave de REPOS
            for key, root in self.roots.items():
                if head.lower() == key.lower():
                    match = self._accept(key, root / rest)
                    return (STATUS_OK, [match]) if match else (STATUS_MISSING, [])
            # 3. nombre del directorio del repo
            for key, root in self.roots.items():
                if head == root.name:
                    match = self._accept(key, root / rest)
                    return (STATUS_OK, [match]) if match else (STATUS_MISSING, [])

        # 4. por basename
        tail = cited.lstrip("./")
        found = [
            (key, path)
            for key, path in self._index().get(parts[-1], [])
            if path.as_posix().endswith("/" + tail)
        ]
        found = [m for m in (self._accept(k, p) for k, p in found) if m]
        if len(found) == 1:
            return STATUS_OK, found
        if found:
            return STATUS_AMBIGUOUS, found
        return STATUS_MISSING, []


# ---------------------------------------------------------------------------
# documentos
# ---------------------------------------------------------------------------


def knowledge_dir(config, project: str) -> Path:
    return Path(config.PROJECTS_DIR) / project / "knowledge"


def find_docs(knowledge_root: Path) -> list[Path]:
    """knowledge/extracted/*.md más cualquier risk_catalog.md bajo knowledge/ (sin duplicar)."""
    docs: list[Path] = []
    extracted = knowledge_root / "extracted"
    if extracted.is_dir():
        docs.extend(sorted(extracted.glob("*.md")))
    if knowledge_root.is_dir():
        for catalog in sorted(knowledge_root.rglob(RISK_CATALOG_NAME)):
            if catalog not in docs:
                docs.append(catalog)
    return docs


def extract_citations(doc: Path, label: str | None = None) -> list[Citation]:
    label = label or doc.name
    out: list[Citation] = []
    section_risk = ""  # id del último encabezado "## R-xx …": cubre las citas de su sección
    for number, text in enumerate(doc.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
        risk = RISK_ID_RE.search(text)
        if text.lstrip().startswith("#"):
            section_risk = risk.group(0) if risk else ""
        for match in CITATION_RE.finditer(text):
            path, start, end = match.group(1), match.group(2), match.group(3)
            out.append(
                Citation(
                    doc=label,
                    md_line=number,
                    raw=match.group(0),
                    path=path,
                    line=int(start) if start else None,
                    line_end=int(end) if end else None,
                    risk_id=risk.group(0) if risk else section_risk,
                )
            )
    return out


def _symbols_on_line(text: str) -> list[str]:
    names = []
    for raw in BACKTICK_SYMBOL_RE.findall(text):
        name = raw.split(".")[-1]
        if name and name not in names:
            names.append(name)
    return names


def _check_symbols(citation: Citation, source_lines: list[str], md_text: str) -> None:
    """Aviso de símbolo movido/ausente solo con nombre exacto (identificador en backticks)."""
    if citation.line is None:
        return
    symbols = _symbols_on_line(md_text)
    if not symbols:
        return
    start = max(0, citation.line - 1 - SYMBOL_WINDOW)
    stop = min(len(source_lines), (citation.line_end or citation.line) + SYMBOL_WINDOW)
    window = "\n".join(source_lines[start:stop])
    for name in symbols:
        pattern = re.compile(rf"\b{re.escape(name)}\b")
        if pattern.search(window):
            return  # al menos un símbolo citado sigue cerca: la cita vale
    # ninguno cerca: ¿alguno en otra línea del mismo archivo?
    for name in symbols:
        pattern = re.compile(rf"\b{re.escape(name)}\b")
        hits = [i for i, line in enumerate(source_lines, 1) if pattern.search(line)]
        if hits:
            citation.status = STATUS_SYMBOL_MOVED
            citation.note = f"{name} ahora en línea {hits[0]}"
            return
    citation.status = STATUS_SYMBOL_ABSENT
    citation.note = f"{symbols[0]} no aparece en el archivo"


def check_doc(doc: Path, index: RepoIndex, label: str | None = None) -> list[Citation]:
    citations = extract_citations(doc, label)
    if not citations:
        return []
    md_lines = doc.read_text(encoding="utf-8", errors="ignore").splitlines()
    for citation in citations:
        status, matches = index.resolve(citation.path)
        citation.status = status
        citation.candidates = [index.relative(k, p) for k, p in matches]
        if status != STATUS_OK:
            continue
        key, real = matches[0]
        citation.resolved = index.relative(key, real)
        if citation.line is None:
            continue
        source_lines = real.read_text(encoding="utf-8", errors="ignore").splitlines()
        if citation.line > len(source_lines):
            citation.status = STATUS_OUT_OF_RANGE
            citation.note = f"el archivo tiene {len(source_lines)} líneas"
            continue
        _check_symbols(citation, source_lines, md_lines[citation.md_line - 1])
    return citations


def check_project(config, project: str) -> list[Citation]:
    """Todas las citas del proyecto, ya verificadas contra config.REPOS."""
    index = RepoIndex(getattr(config, "REPOS", {}))
    out: list[Citation] = []
    for doc in find_docs(knowledge_dir(config, project)):
        out.extend(check_doc(doc, index))
    return out


# ---------------------------------------------------------------------------
# reporte
# ---------------------------------------------------------------------------


def broken(citations: list[Citation]) -> list[Citation]:
    return [c for c in citations if c.broken]


def summarize(citations: list[Citation]) -> dict[str, Counter]:
    by_doc: dict[str, Counter] = defaultdict(Counter)
    for c in citations:
        by_doc[c.doc]["total"] += 1
        by_doc[c.doc][c.status] += 1
    return by_doc


def format_report(citations: list[Citation], max_items: int = 40) -> str:
    """Conteos por documento y detalle de rotas (con id de riesgo) y avisos."""
    if not citations:
        return "Sin citas archivo:línea en el knowledge del proyecto."
    lines: list[str] = []
    order = (STATUS_OK, *BROKEN_STATUSES, *WARNING_STATUSES)
    for doc, counts in sorted(summarize(citations).items()):
        parts = [f"{status}={counts[status]}" for status in order if counts[status]]
        lines.append(f"{doc}: total={counts['total']} " + " ".join(parts))

    rotas = broken(citations)
    avisos = [c for c in citations if c.warning]
    lines.append("")
    lines.append(
        f"Citas: {len(citations)} · rotas: {len(rotas)} · avisos: {len(avisos)} · "
        f"ok: {sum(1 for c in citations if c.status == STATUS_OK)}"
    )
    if rotas:
        lines.append("")
        lines.append("Rotas:")
        for c in rotas[:max_items]:
            lines.append(f"  {_item(c)}")
        if len(rotas) > max_items:
            lines.append(f"  … y {len(rotas) - max_items} más")
    if avisos:
        lines.append("")
        lines.append("Avisos (no afectan el resultado):")
        for c in avisos[:max_items]:
            lines.append(f"  {_item(c)}")
        if len(avisos) > max_items:
            lines.append(f"  … y {len(avisos) - max_items} más")
    return "\n".join(lines)


def _item(c: Citation) -> str:
    rid = f" {c.risk_id}" if c.risk_id else ""
    extra = ""
    if c.status == STATUS_AMBIGUOUS:
        extra = f" → {len(c.candidates)} candidatas: " + ", ".join(c.candidates[:3])
    elif c.note:
        extra = f" ({c.note})"
    return f"{c.doc}:{c.md_line}{rid} [{c.status}] {c.raw}{extra}"
