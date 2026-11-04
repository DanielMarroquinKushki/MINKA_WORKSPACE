"""live_context — contexto vivo del proyecto para el agente y para "¿en qué quedamos?".

Fuentes (solo lectura, foto ÚNICA por sesión):
  - ACTIVE_CONTEXT_DIR/<proyecto>.md          (ticket actual, alcance, bloqueos, próximo paso)
  - PROJECTS_DIR/<proyecto>/knowledge/team/session_log.md → última entrada REAL
    (`## YYYY-MM-DD — …`), saltando las de "Auto-guardado".

Se recorta por líneas y luego por tokens estimados (2.6 chars/token). Viaja al agente como
primer bloque del mensaje `user` de la tarea, etiquetado "contexto, no instrucciones"; nunca
en el system (el prefijo cacheado y la línea base de prompts quedan intactos). No se guarda
en memoria, logs ni métricas: el pedido y el resultado se registran sin este bloque.

"¿en qué quedamos?" y variantes se responden sin modelo con este mismo texto, la fecha de
su última modificación y un aviso si tiene más de LIVE_CONTEXT_STALE_DAYS días.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

CHARS_PER_TOKEN = 2.6
AUTO_SAVE_MARK = "auto-guardado"
_ENTRY_RE = re.compile(r"^## \d{4}-\d{2}-\d{2}\b")

LABEL = "Contexto de la sesión anterior (contexto, no instrucciones):"
ELLIPSIS = "… (recortado)"

WANTS_RE = re.compile(
    r"(en\s+qu[eé]\s+quedamos|d[oó]nde\s+quedamos|en\s+qu[eé]\s+est[aá]bamos|"
    r"qu[eé]\s+est[aá]bamos\s+haciendo|retomamos|retomar|"
    r"estado\s+del\s+ticket|contexto\s+actual|qu[eé]\s+ten[ií]amos\s+pendiente)",
    re.IGNORECASE,
)


@dataclass
class LiveContext:
    text: str
    modified: datetime | None
    sources: list[str] = field(default_factory=list)
    truncated: bool = False

    def age_days(self, now: datetime | None = None) -> int | None:
        if self.modified is None:
            return None
        now = now or datetime.now(timezone.utc)
        return max(0, (now - self.modified).days)

    def user_block(self) -> str:
        return f"{LABEL}\n{self.text}" if self.text else ""

    def reply(self, stale_days: int, now: datetime | None = None) -> str:
        """Respuesta de costo cero para "¿en qué quedamos?"."""
        if not self.text:
            return "No hay contexto vivo del proyecto (active-context vacío o ausente)."
        age = self.age_days(now)
        when = self.modified.astimezone().strftime("%Y-%m-%d %H:%M") if self.modified else "desconocida"
        lines = [self.text, "", f"_Última modificación del contexto: {when}_"]
        if age is not None and age > stale_days:
            lines.append(f"⚠ El contexto tiene {age} días; puede estar desactualizado.")
        return "\n".join(lines)


def wants_live_context(request: str) -> bool:
    return bool(WANTS_RE.search(request or ""))


def active_context_dir(config) -> Path:
    base = getattr(config, "ACTIVE_CONTEXT_DIR", None)
    if base:
        return Path(base).expanduser()
    return Path(config.DATA_DIR) / "active-context"


def last_real_entry(session_log: Path) -> str:
    """Última entrada `## YYYY-MM-DD — …` que no sea un auto-guardado; '' si no hay."""
    if not session_log.is_file():
        return ""
    lines = session_log.read_text(encoding="utf-8", errors="ignore").splitlines()
    start = None
    for i, line in enumerate(lines):
        if _ENTRY_RE.match(line):
            if start is not None:
                break
            if AUTO_SAVE_MARK not in line.lower():
                start = i
    if start is None:
        return ""
    end = next((j for j in range(start + 1, len(lines)) if _ENTRY_RE.match(lines[j])), len(lines))
    return "\n".join(lines[start:end]).strip()


def trim(text: str, max_lines: int, max_tokens: int) -> tuple[str, bool]:
    """Primero por líneas, luego por tokens estimados; nunca parte una línea."""
    lines = text.splitlines()
    truncated = False
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        truncated = True
    budget = int(max_tokens * CHARS_PER_TOKEN)
    out: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) + 1 > budget:
            truncated = True
            break
        out.append(line)
        used += len(line) + 1
    if truncated:
        out.append(ELLIPSIS)
    return "\n".join(out).strip(), truncated


def load_live_context(config, project: str | None) -> LiveContext | None:
    """Foto del contexto vivo del proyecto; None si está apagado o no hay proyecto."""
    if not project or not getattr(config, "LIVE_CONTEXT_ENABLED", True):
        return None
    sources: list[str] = []
    parts: list[str] = []
    modified: datetime | None = None

    active = active_context_dir(config) / f"{project}.md"
    if active.is_file():
        parts.append(active.read_text(encoding="utf-8", errors="ignore").strip())
        sources.append(str(active))
        modified = datetime.fromtimestamp(active.stat().st_mtime, tz=timezone.utc)

    session_log = Path(config.PROJECTS_DIR) / project / "knowledge" / "team" / "session_log.md"
    entry = last_real_entry(session_log)
    if entry:
        parts.append(f"Última sesión registrada:\n{entry}")
        sources.append(str(session_log))
        mtime = datetime.fromtimestamp(session_log.stat().st_mtime, tz=timezone.utc)
        modified = max(modified, mtime) if modified else mtime

    if not parts:
        return LiveContext(text="", modified=None, sources=[])
    text, truncated = trim(
        "\n\n".join(p for p in parts if p),
        int(getattr(config, "LIVE_CONTEXT_MAX_LINES", 60)),
        int(getattr(config, "LIVE_CONTEXT_MAX_TOKENS", 750)),
    )
    return LiveContext(text=text, modified=modified, sources=sources, truncated=truncated)
