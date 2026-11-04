"""conversation_memory — historial ÚNICO de la sesión, compartido por todos los agentes.

Reemplaza al bloque REPL de main.py y a la SessionMemory por agente: cada turno se guarda
una sola vez (pedido crudo, agentes, resultado recortado, citas) y viaja al agente como pares
user/assistant. Solo en memoria: no se persiste ni entra en métricas. /clear la vacía.

El recorte se hace UNA vez al guardar y es determinista: así el historial es byte-estable
entre turnos y el marcador móvil de caché lo convierte en lectura.
"""

import re
from dataclasses import dataclass, field

CITATION_RE = re.compile(r"[\w./-]+\.\w{1,5}:\d+")
_HEADER_RE = re.compile(r"^### \[[\w]+\]\s*$", re.MULTILINE)
_ELLIPSIS = "…"


def strip_agent_headers(text: str) -> str:
    """Quita las cabeceras `### [agente]` de la salida consolidada (son presentación)."""
    return re.sub(r"\n{3,}", "\n\n", _HEADER_RE.sub("", text or "")).strip()


def citations_in(text: str) -> list[str]:
    return sorted(set(CITATION_RE.findall(text or "")))


def trim_result(text: str, limit: int) -> str:
    """Resultado corto → entero. Largo → primer párrafo + líneas con cita + último párrafo,
    sin partir nunca una línea con cita; si aun así no cabe, se acortan los párrafos."""
    text = strip_agent_headers(text)
    if len(text) <= limit:
        return text
    paragraphs = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    first, last = paragraphs[0], (paragraphs[-1] if len(paragraphs) > 1 else "")
    middle = "\n".join(paragraphs[1:-1]) if len(paragraphs) > 2 else ""
    cites = [ln for ln in middle.splitlines() if CITATION_RE.search(ln) and ln not in first and ln not in last]
    cite_block = "\n".join(cites)
    # Presupuesto para los párrafos: lo que quede tras las citas (que no se parten).
    budget = max(limit - len(cite_block) - 2 * len(_ELLIPSIS) - 4, 0)
    if len(first) + len(last) > budget:
        half = budget // 2
        first = _cut(first, half) if len(first) > half else first
        last = _cut(last, budget - len(first)) if len(last) > budget - len(first) else last
    parts = [first]
    if cite_block:
        parts += [_ELLIPSIS, cite_block]
    if last:
        parts += [_ELLIPSIS, last]
    out = "\n".join(p for p in parts if p)
    if len(out) > limit:  # solo citas y ya no caben todas: conservar las primeras enteras
        kept, size = [], 0
        for ln in cites:
            if size + len(ln) + 1 > limit:
                break
            kept.append(ln)
            size += len(ln) + 1
        out = "\n".join(kept) or _cut(first, limit)
    return out


def _cut(paragraph: str, limit: int) -> str:
    """Acorta un párrafo sin partir una línea con cita (corta por líneas)."""
    if len(paragraph) <= limit:
        return paragraph
    out, size = [], 0
    for ln in paragraph.splitlines():
        if size + len(ln) + 1 > limit:
            if CITATION_RE.search(ln) or out:
                break
            return ln[: max(limit - 1, 0)].rstrip() + _ELLIPSIS
        out.append(ln)
        size += len(ln) + 1
    return "\n".join(out).rstrip() + _ELLIPSIS


@dataclass
class Turn:
    request: str
    agents: list[str]
    result: str  # recortado, sin cabeceras
    useful: bool
    citations: list[str] = field(default_factory=list)


class ConversationMemory:
    def __init__(self, max_turns: int = 5, result_chars: int = 1500):
        self.max_turns = max_turns
        self.result_chars = result_chars
        self._turns: list[Turn] = []

    def add(self, request: str, agents: list[str], result: str, useful: bool) -> Turn:
        trimmed = trim_result(result, self.result_chars) if result else ""
        turn = Turn(request, list(agents), trimmed, useful, citations_in(result))
        self._turns.append(turn)
        return turn

    def clear(self) -> None:
        self._turns.clear()

    @property
    def turns(self) -> list[Turn]:
        return list(self._turns)

    @property
    def last(self) -> Turn | None:
        return self._turns[-1] if self._turns else None

    def as_messages(self, n: int | None = None) -> list[dict]:
        """Últimos n turnos útiles (con agente y resultado) como pares user/assistant."""
        useful = [t for t in self._turns if t.useful and t.agents and t.result]
        messages = []
        for t in useful[-(n or self.max_turns) :]:
            messages.append({"role": "user", "content": t.request})
            messages.append({"role": "assistant", "content": t.result})
        return messages

    def planner_context(self, n: int = 3, chars: int = 300) -> str:
        """Resumen compacto para resolver seguimientos (el planner no enruta sobre él)."""
        useful = [t for t in self._turns if t.useful and t.agents][-n:]
        if not useful:
            return ""
        lines = []
        for i, t in enumerate(useful, 1):
            lines.append(f"[Turno {i}] Dev: {t.request[:chars]}")
            lines.append(f"[Turno {i}] Agente ({', '.join(a.removesuffix('_agent') for a in t.agents)}): {t.result[:chars]}")
        return "\n".join(lines)

    def __len__(self) -> int:
        return len(self._turns)
