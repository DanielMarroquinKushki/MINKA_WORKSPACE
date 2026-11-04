"""response_profile — concisión por tipo de pedido y recorte determinista del cierre.

Perfiles (los decide el orquestador a partir del router, nunca el agente):
  breve     consulta puntual (router info_direct): respuesta directa, ~12 líneas, con
            TODAS las citas archivo:línea y sin ofrecer más trabajo al final.
  completo  investigación (planner), agente forzado (--agent, bridge), fix-loop, resume.
  reporte   report_agent: conserva su formato propio.

El perfil viaja como bloque al final del mensaje `user` del agente, nunca en el system:
así el prefijo cacheado (tools + system) y la línea base de prompts quedan byte-idénticos.
"en detalle" / "completo" en el pedido, `--completo` y `--debug` fuerzan el modo completo.

El cierre "¿Quieres que…?" se recorta después de la respuesta (gancho `_record_turn`):
solo la última línea no vacía, solo si empieza por uno de OFFER_PATTERNS y termina en "?",
y nunca si contiene una cita archivo:línea. La nota "respuesta acotada" la añade el código
al texto mostrado; no va a la memoria ni al modelo.
"""

from __future__ import annotations

import re

from minka.core.memory.conversation_memory import CITATION_RE

PROFILE_BRIEF = "breve"
PROFILE_FULL = "completo"
PROFILE_REPORT = "reporte"

BRIEF_ROUTERS = ("info_direct",)
REPORT_AGENTS = ("report_agent",)

BRIEF_BLOCK = (
    "---\n"
    "Formato de respuesta (consulta puntual): responde directo, en unas 12 líneas como máximo. "
    "Conserva TODAS las citas archivo:línea. Usa tabla solo si comparas opciones. "
    "No termines ofreciendo más trabajo (nada de \"¿Quieres que…?\")."
)

BOUNDED_NOTE = "_Respuesta acotada; pide 'en detalle' para ampliar._"

# Lista cerrada: solo estas aperturas cuentan como "oferta de más trabajo".
OFFER_PATTERNS = ("¿quieres", "¿te gustaría", "¿deseas", "¿necesitas", "¿te sirve")

_FULL_RE = re.compile(r"\ben detalle\b|\bcompleto\b", re.IGNORECASE)
_LINE_DECOR_RE = re.compile(r"^[\s>*_\-–•]+|[\s*_]+$")


def wants_full(request: str) -> bool:
    """El dev pide el modo completo en el propio pedido."""
    return bool(_FULL_RE.search(request or ""))


def profile_for(router_name: str | None, agent: str, force_full: bool = False) -> str:
    if agent in REPORT_AGENTS:
        return PROFILE_REPORT
    if force_full:
        return PROFILE_FULL
    if router_name in BRIEF_ROUTERS:
        return PROFILE_BRIEF
    return PROFILE_FULL


def user_block(profile: str) -> str:
    """Texto que se añade al mensaje user; vacío salvo en el perfil breve."""
    return BRIEF_BLOCK if profile == PROFILE_BRIEF else ""


def is_offer_line(line: str) -> bool:
    """Última línea que ofrece más trabajo: patrón cerrado, termina en '?', sin cita."""
    plain = _LINE_DECOR_RE.sub("", line or "").strip()
    if not plain.endswith("?") or CITATION_RE.search(plain):
        return False
    return plain.lower().startswith(OFFER_PATTERNS)


def strip_closing_offer(text: str) -> tuple[str, bool, int]:
    """(texto, ends_with_offer, closings_removed). Quita como máximo la última línea."""
    if not text:
        return text, False, 0
    lines = text.rstrip().split("\n")
    idx = len(lines) - 1
    while idx >= 0 and not lines[idx].strip():
        idx -= 1
    if idx < 0 or not is_offer_line(lines[idx]):
        return text, False, 0
    kept = "\n".join(lines[:idx]).rstrip()
    return kept, True, 1


def with_bounded_note(text: str, profile: str) -> str:
    """Solo para mostrar: avisa que la respuesta fue acotada por el perfil breve."""
    if profile != PROFILE_BRIEF or not text:
        return text
    return f"{text.rstrip()}\n\n{BOUNDED_NOTE}"
