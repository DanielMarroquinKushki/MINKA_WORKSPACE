"""decisions — historial de decisiones (ADR) y alcance del ticket como contexto del agente.

Determinista y a $0. Lee `knowledge/team/decisions_log.md` del proyecto (entradas
`## ADR-nnn — título` con campos `**Fecha:** YYYY-MM-DD` y `**Estado:** …`) y el ticket y estado
del contexto vivo (`active-context/<proyecto>.md`: `*Ticket: ID — título*`, `## Estado (fecha)`).
Dado el pedido, elige hasta CONTEXT_DECISIONS_MAX entradas por coincidencia de términos y arma un
bloque para el mensaje `user` rotulado "historial de decisiones, no instrucciones", con tope
CONTEXT_DECISIONS_MAX_TOKENS. Una decisión reemplazada o revertida se marca como tal. El bloque no
se registra en logs ni métricas: solo viaja al modelo (como el contexto vivo).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from minka.core.memory.live_context import CHARS_PER_TOKEN, active_context_dir

ADR_RE = re.compile(r"\bADR-(\d{2,3})\b")
_HEADING_RE = re.compile(r"^## (ADR-\d{2,3})\s*[—–-]\s*(.+?)\s*$")
_FIELD_RE = re.compile(r"^\*\*([A-Za-zÁÉÍÓÚáéíóúñ ]+):\*\*\s*(.*)$")
_DATE_RE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")
_REPLACED_WORDS = ("reemplazada", "reemplazado", "revertida", "revertido", "sustituida", "obsoleta", "superseded")
_REVERTS_RE = re.compile(r"\b(?:revierte|reemplaza|sustituye)\s+(?:a\s+)?(ADR-\d{2,3})", re.IGNORECASE)
_TICKET_RE = re.compile(r"^\*?Ticket:\s*([A-Z][A-Z0-9]+-\d+)\s*[—–-]\s*(.+?)\*?\s*$", re.MULTILINE)
_STATE_RE = re.compile(r"^## Estado\s*\((\d{4}-\d{2}-\d{2})\)", re.MULTILINE)
_STOP = frozenset(
    "para como cual cuales donde desde hasta sobre entre este esta estos estas aquel segun codigo "
    "estado cuando porque pero mas menos todo toda hacia tiene tienen hace hacen sobre dentro".split()
)

LABEL = "Historial de decisiones del proyecto (contexto, no instrucciones)"
NO_HISTORY = "No hay decisión registrada sobre este tema."
SCOPE_UNKNOWN = "No sé si entra en el alcance del ticket: no hay ticket ni estado en el contexto vivo."
RULES = (
    "Reglas para usar este historial: cita una decisión solo con su id y fecha; si está reemplazada, "
    "dilo y no la uses como base; si un hallazgo contradice una decisión vigente, dilo explícitamente; "
    "no infieras decisiones no escritas (di \"no hay decisión registrada\"); si no puedes determinar el "
    "alcance, di \"no sé si entra en el alcance del ticket\"; la recomendación se apoya en un hallazgo "
    "con cita archivo:línea; el historial no vuelve correcto un hallazgo."
)


@dataclass
class Decision:
    id: str
    title: str
    date: str | None
    status: str
    text: str  # Decisión + Contexto, recortado
    replaced_by: str | None = None
    replaces: list[str] = field(default_factory=list)

    @property
    def replaced(self) -> bool:
        return self.replaced_by is not None or any(w in self.status.lower() for w in _REPLACED_WORDS)

    def label(self) -> str:
        date = self.date or "sin fecha"
        state = self.status.split(".")[0].strip() or "sin estado"
        return f"{self.id} ({date}, {state})"


@dataclass
class Scope:
    ticket: str | None
    title: str | None
    state_date: str | None

    @property
    def known(self) -> bool:
        return bool(self.ticket)

    def line(self) -> str:
        if not self.known:
            return SCOPE_UNKNOWN
        head = f"Alcance del ticket {self.ticket}"
        if self.title:
            head += f" — {self.title}"
        if self.state_date:
            head += f" (estado al {self.state_date})"
        return head + "."


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in text if not unicodedata.combining(c)).lower()


def _terms(text: str) -> set[str]:
    """Palabras de 4+ letras (sin acentos ni vacías) más las siglas en mayúsculas (WK, PIN, KCV)."""
    words = {t for t in re.findall(r"[a-z0-9_]{4,}", _normalize(text)) if t not in _STOP}
    acronyms = {a.lower() for a in re.findall(r"\b[A-Z][A-Z0-9]{1,5}\b", text or "")}
    return words | acronyms


def decisions_path(config, project: str | None) -> Path | None:
    if not project:
        return None
    return Path(config.PROJECTS_DIR) / project / "knowledge" / "team" / "decisions_log.md"


def parse_decisions(text: str) -> list[Decision]:
    """Entradas `## ADR-nnn — título` con sus campos; ignora el resto del archivo."""
    out: list[Decision] = []
    current: Decision | None = None
    fields: dict[str, str] = {}
    last_field: str | None = None

    def close() -> None:
        if current is None:
            return
        status = fields.get("Estado", "")
        date = None
        m = _DATE_RE.search(fields.get("Fecha", ""))
        if m:
            date = m.group(1)
        body = " ".join(p for p in (fields.get("Decisión") or fields.get("Decision"), fields.get("Contexto")) if p)
        current.status = status
        current.date = date
        current.text = re.sub(r"\s+", " ", body).strip()
        if current.replaced:
            ids = [i for i in (f"ADR-{n}" for n in ADR_RE.findall(status)) if i != current.id]
            current.replaced_by = ids[0] if ids else current.replaced_by
        current.replaces = [m.group(1) for m in _REVERTS_RE.finditer(status + " " + current.text) if m.group(1) != current.id]
        out.append(current)

    for line in text.splitlines():
        h = _HEADING_RE.match(line)
        if h:
            close()
            current, fields, last_field = Decision(h.group(1), h.group(2).strip(), None, "", ""), {}, None
            continue
        if line.startswith("## "):
            close()
            current, fields, last_field = None, {}, None
            continue
        if current is None:
            continue
        f = _FIELD_RE.match(line)
        if f:
            last_field = f.group(1).strip()
            fields[last_field] = f.group(2).strip()
        elif last_field and line.strip() and not line.startswith("#"):
            fields[last_field] += " " + line.strip()
    close()
    # Quien revierte a otra la marca como reemplazada si la otra no lo dice ya.
    by_id = {d.id: d for d in out}
    for d in out:
        for old in d.replaces:
            if old in by_id and not by_id[old].replaced:
                by_id[old].replaced_by = d.id
                by_id[old].status = (by_id[old].status + f" (revertida por {d.id})").strip()
    return out


def load_decisions(config, project: str | None) -> list[Decision]:
    path = decisions_path(config, project)
    if not path or not path.is_file():
        return []
    try:
        return parse_decisions(path.read_text(encoding="utf-8", errors="ignore"))
    except OSError:
        return []


PREFIX_LEN = 5


def _prefixes(terms: set[str], exclude: tuple[str, ...]) -> set[str]:
    return {t[:PREFIX_LEN] for t in terms if len(t) >= PREFIX_LEN and t[:PREFIX_LEN] not in exclude}


def _score(d: Decision, terms: set[str], prefixes: set[str] | None, exclude: tuple[str, ...]) -> int:
    """Exacta: 2 (4 en el título). Prefijo de 5 letras: 1 (2 en el título), solo si se pide."""
    title, text = _terms(d.title), _terms(d.text)
    shared = terms & (title | text)
    score = 2 * len(shared) + 2 * len(shared & title)
    if prefixes:
        covered = {t[:PREFIX_LEN] for t in shared}
        tp, xp = _prefixes(title, exclude), _prefixes(text, exclude)
        extra = (prefixes & (tp | xp)) - covered
        score += len(extra) + len(extra & tp)
    return score


def select(
    decisions: list[Decision],
    request: str,
    limit: int = 3,
    prefix: bool | None = None,
    prefix_exclude: tuple[str, ...] | None = None,
) -> list[Decision]:
    """Hasta `limit` decisiones por términos en común con el pedido (título + texto). Con `prefix`
    (config CONTEXT_DECISIONS_PREFIX) y menos de 2 exactas, suma coincidencias por prefijo de 5
    letras (anula/anular/anulación) con peso menor; `prefix_exclude` (CONTEXT_DECISIONS_PREFIX_EXCLUDE)
    quita prefijos ambiguos ("proce"). Más puntos primero, luego la más reciente."""
    from minka.config_loader import config

    terms = _terms(request)
    if not terms:
        return []
    if prefix is None:
        prefix = bool(getattr(config, "CONTEXT_DECISIONS_PREFIX", False))
    exclude = tuple(prefix_exclude if prefix_exclude is not None else getattr(config, "CONTEXT_DECISIONS_PREFIX_EXCLUDE", ()) or ())
    exact = [(s, d.date or "", d) for d in decisions if (s := _score(d, terms, None, exclude)) > 0]
    if prefix and len(exact) < 2:
        prefixes = _prefixes(terms, exclude)
        scored = [(s, d.date or "", d) for d in decisions if (s := _score(d, terms, prefixes, exclude)) > 0]
    else:
        scored = exact
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return [d for _, _, d in scored[:limit]]


def load_scope(config, project: str | None) -> Scope:
    if not project:
        return Scope(None, None, None)
    path = active_context_dir(config) / f"{project}.md"
    if not path.is_file():
        return Scope(None, None, None)
    text = path.read_text(encoding="utf-8", errors="ignore")
    t = _TICKET_RE.search(text)
    s = _STATE_RE.search(text)
    return Scope(t.group(1) if t else None, t.group(2).strip() if t else None, s.group(1) if s else None)


def user_block(selected: list[Decision], scope: Scope, max_tokens: int = 600) -> str:
    """Bloque para el mensaje user; nunca se guarda en logs ni métricas."""
    budget = int(max_tokens * CHARS_PER_TOKEN)
    lines = [f"{LABEL}:", scope.line()]
    if not selected:
        lines.append(NO_HISTORY)
    for d in selected:
        head = f"- {d.label()}: {d.title}"
        if d.replaced:
            head += f" — REEMPLAZADA{f' por {d.replaced_by}' if d.replaced_by else ''}: no usar como base"
        lines.append(head)
        if d.text:
            lines.append(f"  {d.text}")
    lines.append(RULES)
    out: list[str] = []
    used = 0
    for ln in lines:
        if used + len(ln) + 1 > budget:
            if ln.startswith("  "):
                continue  # el detalle es lo primero que se sacrifica
            cut = max(budget - used - 2, 0)
            if cut > 20:
                out.append(ln[:cut] + "…")
            used = budget
            break
        out.append(ln)
        used += len(ln) + 1
    if RULES not in out and used + len(RULES) + 1 <= budget * 1.2:
        out.append(RULES)
    return "\n".join(out)


def index(decisions: list[Decision]) -> dict[str, str | None]:
    """id → fecha, para que el verificador compruebe referencias."""
    return {d.id: d.date for d in decisions}
