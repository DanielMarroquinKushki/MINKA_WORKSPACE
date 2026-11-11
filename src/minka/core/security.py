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


def scan_tree(root: Path, extra_rules: list | None = None) -> list[dict]:
    """Hallazgos `{path, line, kind, level}` sobre los archivos de texto bajo `root`.
    level = "fallo" (alta confianza) | "revisar" (hex/base64 largos: pueden ser hashes o KCV)."""
    from minka.core.knowledge.extractor import scan_secrets

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
        rel = f.relative_to(root).as_posix()
        for hit in scan_secrets(text, rel, extra_rules):
            m = re.match(r"^(.*):(\d+) \(([A-Z_]+)\)$", hit)
            if not m:
                continue
            kind = m.group(3)
            level = "fallo" if kind in HIGH_CONFIDENCE_IDS or kind not in REVIEW_IDS else "revisar"
            hits.append({"path": m.group(1), "line": int(m.group(2)), "kind": kind, "level": level})
    return hits


def scan_text(text: str, label: str, extra_rules: list | None = None) -> list[dict]:
    """Igual que scan_tree pero sobre un texto ya leído (archivos staged del pre-commit)."""
    from minka.core.knowledge.extractor import scan_secrets

    out = []
    for hit in scan_secrets(text, label, extra_rules):
        m = re.match(r"^(.*):(\d+) \(([A-Z_]+)\)$", hit)
        if m:
            kind = m.group(3)
            out.append({"path": m.group(1), "line": int(m.group(2)), "kind": kind,
                        "level": "revisar" if kind in REVIEW_IDS else "fallo"})
    return out


def format_scan(hits: list[dict]) -> str:
    if not hits:
        return "Sin hallazgos."
    lines = [f"  {h['path']}:{h['line']} ({h['kind']}) [{h['level']}]" for h in hits]
    fails = sum(1 for h in hits if h["level"] == "fallo")
    lines.append(f"{len(hits)} hallazgo(s): {fails} fallo(s), {len(hits) - fails} a revisar.")
    return "\n".join(lines)
