"""claim_check — verificador determinista de afirmaciones de la respuesta (sin modelo).

Mira tres cosas: identificadores entre backticks, bloques de código ``` y cifras relevantes
(fechas, hex/KCV, ids largos, plazos). Cada una debe tener respaldo en el pedido, en el resultado
de los pasos previos del mismo plan o en los repos (`git grep` literal, con fallback a grep).
Lo que no lo tiene se anota en la misma línea: `⚠ sin respaldo: X, Y`. Un bloque de código bajo
un encabezado de propuesta (propuesta, recomendación, sugerencia, plan, próximos pasos, ejemplo)
o con lenguaje diff/patch se marca `(propuesto)` y no cuenta como fallo. Si la última respuesta
del agente se cortó por MAX_TOKENS, se añade una línea de aviso.

Corre por paso en el orquestador (antes de guardar el resultado) con VERIFY_CLAIMS_ENABLED. Las
citas `archivo:línea` las verifica citation_check; aquí se ignoran.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from minka.core.tools.analysis import citations as cit

MARK_UNSUPPORTED = "⚠ sin respaldo"
MARK_DOC = "(doc"
MARK_DOC_BLOCK = "_(doc)_"
MARK_PROPOSED = "_(propuesto)_"
MARK_TRUNCATED = "_⚠ Respuesta cortada por tope de tokens (MAX_TOKENS)._"

MAX_TOKENS_CHECKED = 120  # tope de consultas a los repos por respuesta
GREP_TIMEOUT = 10
BLOCK_MIN_SUPPORT = 0.6  # fracción de identificadores del bloque que debe tener respaldo

_FENCE_RE = re.compile(r"^\s*(```|~~~)\s*([\w+-]*)")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*)")
_PROPOSED_RE = re.compile(
    r"propuest|recomend|sugeren|sugerid|siguientes? pasos?|pr[oó]ximos? pasos?|\bplan\b|ejemplo|"
    r"c[oó]mo (deber[ií]a|podr[ií]a)|refactor|a implementar",
    re.IGNORECASE,
)
_PROPOSED_LANGS = ("diff", "patch")
_BACKTICK_RE = re.compile(r"`([^`\n]{3,120})`")
_IDENT_SHAPE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:[.:/-][A-Za-z0-9_]+)*(?:\(\))?$")
_WORD_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b")
_KEYWORDS = frozenset(
    "func return if else for range package import type struct interface error string int int64 "
    "bool nil true false var const map chan go defer switch case default break continue select "
    "def class None True False self print lambda with as pass raise try except finally import from "
    "while elif yield async await public private static void new this null let function export "
    "fmt err ctx context log len cap make append errors strings time json http".split()
)
_NUMBER_RES = (
    ("fecha", re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}/\d{2,4}\b")),
    ("kcv", re.compile(r"\bKCV\b\s*[:=]?\s*`?([0-9A-Fa-f]{4,6})\b", re.IGNORECASE)),
    ("hex", re.compile(r"\b(?=[0-9A-Fa-f]*[A-Fa-f])(?=[0-9A-Fa-f]*\d)[0-9A-Fa-f]{6,}\b")),
    ("id", re.compile(r"\b\d{6,}\b")),
    ("plazo", re.compile(r"\b\d+\s*(?:d[ií]as?|horas?|minutos?|segundos?|semanas?|meses|a[ñn]os?|ms)\b", re.IGNORECASE)),
)


@dataclass
class ClaimResult:
    text: str
    identifiers_checked: int = 0
    identifiers_unsupported: int = 0
    numbers_checked: int = 0
    numbers_unsupported: int = 0
    blocks_checked: int = 0
    blocks_unsupported: int = 0
    blocks_proposed: int = 0
    truncated: bool = False
    doc_supported: int = 0  # respaldados solo por el knowledge del proyecto
    unsupported_tokens: list[str] = field(default_factory=list)

    @property
    def checked(self) -> int:
        return self.identifiers_checked + self.numbers_checked + self.blocks_checked

    @property
    def unsupported(self) -> int:
        return self.identifiers_unsupported + self.numbers_unsupported + self.blocks_unsupported


SUPPORT_PREV, SUPPORT_REPO, SUPPORT_DOC = "prev", "repo", "doc"


class Evidence:
    """Respaldo: pedido + pasos previos, grep literal en los repos (exacto y luego sin
    distinguir mayúsculas: `workingKey` cuenta si el código tiene `WorkingKey`) y, en último
    lugar, el knowledge del proyecto (extracted + learning), que se marca "(doc)"."""

    def __init__(self, index: cit.RepoIndex | None, previous: list[str], docs: str = ""):
        self.roots = list(index.roots.values()) if index is not None else []
        self.previous = "\n".join(p for p in previous if p)
        self.docs = docs or ""
        self.docs_lower = self.docs.lower()
        self._cache: dict[str, str | None] = {}
        self.lookups = 0

    def support(self, token: str) -> str | None:
        """SUPPORT_PREV / SUPPORT_REPO / SUPPORT_DOC, o None si nada lo respalda."""
        if token in self.previous:
            return SUPPORT_PREV
        if token in self._cache:
            return self._cache[token]
        if self.lookups >= MAX_TOKENS_CHECKED:
            return SUPPORT_PREV  # tope alcanzado: no se marca lo que no se pudo comprobar
        self.lookups += 1
        kind = None
        if any(_grep_repo(root, token) for root in self.roots):
            kind = SUPPORT_REPO
        elif any(_grep_repo(root, token, ignore_case=True) for root in self.roots):
            kind = SUPPORT_REPO
        elif token.lower() in self.docs_lower:
            kind = SUPPORT_DOC
        self._cache[token] = kind
        return kind

    def supports(self, token: str) -> bool:
        return self.support(token) is not None


def _grep_repo(root: Path, token: str, ignore_case: bool = False) -> bool:
    flag = ["-i"] if ignore_case else []
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "grep", "-l", "-F", "-I", *flag, "-e", token, "--", "."],
            capture_output=True, text=True, timeout=GREP_TIMEOUT,
        )
        if proc.returncode in (0, 1):
            return proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        proc = subprocess.run(
            ["grep", "-rIlF", *flag, "--exclude-dir=.git", "--exclude-dir=vendor", "--exclude-dir=node_modules",
             "-e", token, "--", str(root)],
            capture_output=True, text=True, timeout=GREP_TIMEOUT,
        )
        return proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def load_project_docs(knowledge_dir: Path | None, subdirs: tuple[str, ...] = ("extracted", "learning")) -> str:
    """Texto de los .md de knowledge/<extracted|learning> (solo lectura; '' si no hay)."""
    if knowledge_dir is None:
        return ""
    parts: list[str] = []
    for sub in subdirs:
        base = Path(knowledge_dir) / sub
        if not base.is_dir():
            continue
        for f in sorted(base.rglob("*.md")):
            try:
                parts.append(f.read_text(encoding="utf-8", errors="ignore"))
            except OSError:
                continue
    return "\n".join(parts)


def _looks_like_identifier(token: str) -> bool:
    if not _IDENT_SHAPE_RE.match(token) or cit.CITATION_RE.fullmatch(token.rstrip("()")):
        return False
    core = token.rstrip("()")
    return "_" in core or "." in core or "/" in core or token.endswith("()") or bool(
        re.search(r"[a-z][A-Z]", core)
    )


def _block_identifiers(code: str) -> set[str]:
    out = set()
    for word in _WORD_RE.findall(code):
        if word in _KEYWORDS or word.lower() in _KEYWORDS:
            continue
        if "_" in word or re.search(r"[a-z][A-Z]", word):
            out.add(word)
    return out


def _numbers_in(line: str, today: str) -> list[str]:
    found = []
    for kind, rx in _NUMBER_RES:
        for m in rx.finditer(line):
            token = m.group(1) if kind == "kcv" else m.group(0)
            if kind == "fecha" and token == today:
                continue
            found.append(token)
    return found


def verify(
    text: str,
    index: cit.RepoIndex | None,
    previous: list[str] | None = None,
    truncated: bool = False,
    today: str | None = None,
    docs: str = "",
) -> ClaimResult:
    """Texto anotado + contadores. Sin texto devuelve el mismo texto."""
    result = ClaimResult(text or "", truncated=truncated)
    if not text:
        return result
    evidence = Evidence(index, previous or [], docs)
    today = today or date.today().isoformat()
    lines = text.split("\n")
    out: list[str] = []
    in_fence = False
    fence_lang = ""
    block: list[str] = []
    heading = ""
    proposed_section = False
    for line in lines:
        fence = _FENCE_RE.match(line)
        if fence and not in_fence:
            in_fence, fence_lang, block = True, fence.group(2).lower(), []
            out.append(line)
            continue
        if fence and in_fence:
            in_fence = False
            out.append(line)
            mark = _check_block(block, fence_lang, proposed_section, evidence, result)
            if mark:
                out.append(mark)
            continue
        if in_fence:
            block.append(line)
            out.append(line)
            continue
        h = _HEADING_RE.match(line)
        if h:
            heading = h.group(1)
            proposed_section = bool(_PROPOSED_RE.search(heading))
        out.append(line if proposed_section else _annotate_line(line, evidence, result, today))
    if truncated:
        out.append("")
        out.append(MARK_TRUNCATED)
    result.text = "\n".join(out)
    return result


def _check_block(block: list[str], lang: str, proposed: bool, evidence: Evidence, result: ClaimResult) -> str | None:
    idents = _block_identifiers("\n".join(block))
    if not idents:
        return None
    if proposed or lang in _PROPOSED_LANGS:
        result.blocks_proposed += 1
        return MARK_PROPOSED
    result.blocks_checked += 1
    kinds = {t: evidence.support(t) for t in idents}
    missing = sorted(t for t, k in kinds.items() if k is None)
    if len(idents) - len(missing) >= BLOCK_MIN_SUPPORT * len(idents):
        if any(k == SUPPORT_DOC for k in kinds.values()):
            result.doc_supported += 1
            return MARK_DOC_BLOCK
        return None
    result.blocks_unsupported += 1
    result.unsupported_tokens.extend(missing)
    shown = ", ".join(missing[:5]) + (", …" if len(missing) > 5 else "")
    return f"{MARK_UNSUPPORTED}: {len(missing)} de {len(idents)} identificadores no aparecen en los repos ni en el resultado previo ({shown})"


def _annotate_line(line: str, evidence: Evidence, result: ClaimResult, today: str) -> str:
    if MARK_UNSUPPORTED in line:
        return line
    missing: list[str] = []
    doc_only: list[str] = []
    for raw in _BACKTICK_RE.findall(line):
        token = raw.strip()
        if not _looks_like_identifier(token):
            continue
        result.identifiers_checked += 1
        core = token.rstrip("()")
        # `pkg.Func` cuenta si existe el nombre final (el código rara vez repite el prefijo).
        tail = re.split(r"[./:]", core)[-1]
        kind = evidence.support(core) or (evidence.support(tail) if tail != core else None)
        if kind is None:
            result.identifiers_unsupported += 1
            missing.append(token)
        elif kind == SUPPORT_DOC:
            doc_only.append(token)
    masked = _BACKTICK_RE.sub(lambda m: " " * len(m.group(0)), line)
    for token in _numbers_in(masked, today):
        result.numbers_checked += 1
        kind = evidence.support(token)
        if kind is None:
            result.numbers_unsupported += 1
            missing.append(token)
        elif kind == SUPPORT_DOC:
            doc_only.append(token)
    if not missing and not doc_only:
        return line
    out = line.rstrip()
    if doc_only:
        result.doc_supported += len(doc_only)
        out = f"{out} {MARK_DOC}: {', '.join(dict.fromkeys(doc_only))})"
    if missing:
        result.unsupported_tokens.extend(missing)
        out = f"{out} {MARK_UNSUPPORTED}: {', '.join(dict.fromkeys(missing))}"
    return out
