"""security — política de archivos excluidos y redacción de secretos, en la capa de herramientas.

Nunca depende del prompt ni del modelo: cada tool de lectura, el indexador, la búsqueda semántica y
el diff consultan `is_excluded()` antes de leer o devolver una ruta; `_execute_tools` pasa cada
resultado por `redact()` antes de que viaje al modelo, y `_log` hace lo mismo antes de escribir en
disco. Nada de esto entra en prompts, schemas de tools ni MODELS: el prompt_hash no cambia.

Exclusión por tipo de archivo (`EXCLUDE_PATTERNS` en config, ampliable por proyecto con
`EXCLUDE_PATTERNS_EXTRA`): un patrón fnmatch se compara con CADA componente de la ruta, así `.aws/`
y `.ssh/` excluyen todo lo que contienen y `.env.local`, `id_rsa.pub` o `secrets.yaml` caen por
nombre. El mensaje al modelo dice solo que está excluido: nunca el contenido.

Redacción: solo patrones de alta confianza (clave de acceso AWS, bloque de llave privada, JWT,
token Bearer y PAN de 13 a 19 dígitos válido por Luhn) → `[REDACTED:TIPO]`. NO se redactan hex,
KCV, alias de llaves ni ids de llave: son el objeto de estudio del análisis de criptografía
(KCV `10E3`, huellas, `alias/...`), no secretos, y borrarlos dejaría ciego al agente. Un hash de
40 hex tampoco es secreto por sí mismo. Contadores por tipo, nunca valores.
"""

from __future__ import annotations

import fnmatch
import re
from collections import Counter
from pathlib import Path

from minka.config_loader import config

EXCLUDED_MESSAGE = "excluido por política de seguridad"

# Patrones (ID, regex) de ALTA confianza para la redacción. El PAN se valida con Luhn aparte.
REDACT_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("PRIVATE_KEY", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)),
    ("PRIVATE_KEY", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[^\n]*")),
    ("AWS_ACCESS_KEY", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("BEARER", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{20,}")),
]
_PAN_RE = re.compile(r"(?<![\w.])\d{13,19}(?![\w.])")

# Hallazgos del escaneo de árboles (knowledge, staged): qué IDs bloquean y cuáles solo se revisan.
HIGH_CONFIDENCE_IDS = frozenset({"AWS_ACCESS_KEY", "PRIVATE_KEY", "JWT", "API_KEY_SK", "CREDENTIAL_ASSIGN"})
REVIEW_IDS = frozenset({"LONG_HEX", "LONG_BASE64"})
# Lista CERRADA de valores de prueba de los manuales: un hallazgo en una línea que los contenga es
# nivel "ejemplo" (ni fallo ni revisar). Solo afecta al escaneo: la redacción en tiempo de ejecución
# (redact) no mira esta lista ni las marcas.
TEST_VALUES: tuple[str, ...] = ("0123456789ABCDEF", "FEDCBA9876543210")
_REPEATED_RE = re.compile(r"(.)\1{15,}")  # 16+ veces el mismo carácter (AAAA…, 0000…)
# Marca manual en el .md: por línea o por bloque. Cuentan aparte para que nada se silencie sin rastro.
MARK_EXAMPLE_LINE = "<!-- ejemplo -->"
MARK_EXAMPLE_START = "<!-- ejemplo:inicio -->"
MARK_EXAMPLE_END = "<!-- ejemplo:fin -->"
SCAN_EXTENSIONS = (".md", ".txt", ".yml", ".yaml", ".json", ".toml", ".py", ".env", ".ini", ".cfg")

_stats: Counter = Counter()
_redactions: Counter = Counter()


# -- exclusión ---------------------------------------------------------------------------------


def exclude_patterns(cfg=None) -> tuple[str, ...]:
    """Lista base (`EXCLUDE_PATTERNS`) + la del proyecto (`EXCLUDE_PATTERNS_EXTRA`)."""
    c = cfg if cfg is not None else config
    base = tuple(getattr(c, "EXCLUDE_PATTERNS", ()) or ())
    extra = tuple(getattr(c, "EXCLUDE_PATTERNS_EXTRA", ()) or ())
    return base + tuple(p for p in extra if p not in base)


def is_excluded(path: str | Path, cfg=None) -> bool:
    """True si algún componente de la ruta coincide con un patrón excluido."""
    parts = Path(str(path)).parts
    patterns = exclude_patterns(cfg)
    return any(fnmatch.fnmatch(part, pat.rstrip("/")) for part in parts for pat in patterns)


def excluded_result(path: str | Path) -> str:
    """Mensaje para el modelo: la ruta está excluida; nunca el contenido."""
    _stats["excluded"] += 1
    return f"ERROR: {Path(str(path)).name} {EXCLUDED_MESSAGE} (EXCLUDE_PATTERNS)."


def note_excluded(n: int = 1) -> None:
    if n > 0:
        _stats["excluded"] += n


def excluded_footer(n: int) -> str:
    return f"\n[{n} archivo(s) omitido(s): {EXCLUDED_MESSAGE}]" if n > 0 else ""


# -- redacción ---------------------------------------------------------------------------------


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = ord(ch) - 48
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def redact(text: str, enabled: bool | None = None) -> tuple[str, Counter]:
    """Sustituye secretos de alta confianza por `[REDACTED:TIPO]`. Devuelve (texto, conteo por tipo)."""
    counts: Counter = Counter()
    if not text:
        return text, counts
    if enabled is None:
        enabled = bool(getattr(config, "SECRET_REDACTION_ENABLED", True))
    if not enabled:
        return text, counts
    out = text
    for kind, rx in REDACT_PATTERNS:
        out, n = rx.subn(f"[REDACTED:{kind}]", out)
        if n:
            counts[kind] += n

    def _pan(m: re.Match) -> str:
        if _luhn_ok(m.group(0)):
            counts["PAN"] += 1
            return "[REDACTED:PAN]"
        return m.group(0)

    out = _PAN_RE.sub(_pan, out)
    if counts:
        _redactions.update(counts)
    return out, counts


def drain_stats() -> dict:
    """Contadores acumulados desde la última llamada: {"excluded": n, "redacted": {tipo: n}}."""
    out = {"excluded": int(_stats.get("excluded", 0)), "redacted": dict(_redactions)}
    _stats.clear()
    _redactions.clear()
    return out


# -- escaneo de árboles (knowledge, pre-commit) ---------------------------------------------------


def example_lines(text: str) -> dict[int, str]:
    """Líneas (1-indexed) marcadas como ejemplo y por qué: "marca" (comentario) o "lista" (TEST_VALUES)."""
    out: dict[int, str] = {}
    in_block = False
    for n, line in enumerate(text.splitlines(), 1):
        if MARK_EXAMPLE_START in line:
            in_block = True
        if in_block or MARK_EXAMPLE_LINE in line:
            out[n] = "marca"
        elif any(v in line for v in TEST_VALUES) or _REPEATED_RE.search(line):
            out[n] = "lista"
        if MARK_EXAMPLE_END in line:
            in_block = False
    return out


def _classify(text: str, label: str, extra_rules: list | None) -> list[dict]:
    from minka.core.knowledge.extractor import scan_secrets

    examples = example_lines(text)
    out: list[dict] = []
    for hit in scan_secrets(text, label, extra_rules):
        m = re.match(r"^(.*):(\d+) \(([A-Z_]+)\)$", hit)
        if not m:
            continue
        kind, n = m.group(3), int(m.group(2))
        if n in examples:
            level, reason = "ejemplo", examples[n]
        else:
            level, reason = ("revisar" if kind in REVIEW_IDS else "fallo"), ""
        out.append({"path": m.group(1), "line": n, "kind": kind, "level": level, "example": reason})
    return out


def scan_tree(root: Path, extra_rules: list | None = None) -> list[dict]:
    """Hallazgos `{path, line, kind, level, example}` sobre los archivos de texto bajo `root`.
    level = "fallo" (alta confianza) | "revisar" (hex/base64 largos) | "ejemplo" (TEST_VALUES o
    marca `<!-- ejemplo -->`; `example` dice si fue "lista" o "marca")."""
    hits: list[dict] = []
    root = Path(root)
    if not root.is_dir():
        return hits
    for f in sorted(root.rglob("*")):
        if not f.is_file() or f.suffix.lower() not in SCAN_EXTENSIONS and not f.name.startswith(".env"):
            continue
        if any(part in (".git", "__pycache__", "node_modules") for part in f.parts):
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        hits.extend(_classify(text, f.relative_to(root).as_posix(), extra_rules))
    return hits


def scan_text(text: str, label: str, extra_rules: list | None = None) -> list[dict]:
    """Igual que scan_tree pero sobre un texto ya leído (archivos staged del pre-commit)."""
    return _classify(text, label, extra_rules)


def scan_summary(hits: list[dict]) -> dict:
    fails = sum(1 for h in hits if h["level"] == "fallo")
    review = sum(1 for h in hits if h["level"] == "revisar")
    by_mark = sum(1 for h in hits if h["level"] == "ejemplo" and h.get("example") == "marca")
    by_list = sum(1 for h in hits if h["level"] == "ejemplo" and h.get("example") == "lista")
    return {"total": len(hits), "fails": fails, "review": review, "example_mark": by_mark, "example_list": by_list}


def format_scan(hits: list[dict]) -> str:
    if not hits:
        return "Sin hallazgos."
    lines = [
        f"  {h['path']}:{h['line']} ({h['kind']}) [{h['level']}{' por ' + h['example'] if h.get('example') else ''}]"
        for h in hits
    ]
    s = scan_summary(hits)
    lines.append(
        f"{s['total']} hallazgo(s): {s['fails']} fallo(s), {s['review']} a revisar, "
        f"{s['example_mark'] + s['example_list']} ejemplo(s) ({s['example_mark']} por marca, {s['example_list']} por lista)."
    )
    return "\n".join(lines)
