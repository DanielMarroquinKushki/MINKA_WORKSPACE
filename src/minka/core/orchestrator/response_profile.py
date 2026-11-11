"""response_profile — concisión por tipo de pedido y recorte determinista del cierre.

Perfiles (los decide el orquestador a partir del router, nunca el agente):
  breve        consulta puntual (router info_direct): respuesta directa, ~12 líneas, con
               TODAS las citas archivo:línea y sin ofrecer más trabajo al final.
  exploratorio planner (router llm) con plan de solo architecture_agent: resumen de 5 a 10
               líneas con la conclusión y luego "Evidencia" con las citas clave archivo:línea.
  completo     investigación multi-agente, agente forzado (--agent, bridge), fix-loop, resume.
  reporte      report_agent: redacta solo sobre el resultado previo (o lo leído con sus tools),
               sin código ni cifras que no estén ahí, "sin evidencia" donde falte, ~60 líneas.

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
PROFILE_EXPLORE = "exploratorio"
PROFILE_DECISION = "decision"  # último paso de un plan de varios agentes
PROFILE_FULL = "completo"
PROFILE_REPORT = "reporte"

BRIEF_ROUTERS = ("info_direct",)
# Exploratorio: pasó por el planner (router llm) y el plan es solo architecture_agent.
EXPLORE_ROUTERS = ("llm",)
EXPLORE_AGENTS = ("architecture_agent",)
REPORT_AGENTS = ("report_agent",)

BRIEF_BLOCK = (
    "---\n"
    "Formato de respuesta (consulta puntual): responde directo, en unas 12 líneas como máximo. "
    "Conserva TODAS las citas archivo:línea. Usa tabla solo si comparas opciones. "
    "No termines ofreciendo más trabajo (nada de \"¿Quieres que…?\").\n"
    "Forma, solo las partes que apliquen: 1) respuesta directa; 2) evidencia con citas "
    "archivo:línea; 3) riesgo o salvedad si existe; 4) siguiente paso solo si el pedido lo implica."
)

_DECISION_SHAPE = (
    "---\n"
    "Formato de respuesta final (30 a 50 líneas), en este orden y solo estas secciones:\n"
    "1) Conclusión: 3 a 5 líneas.\n"
    "2) Hallazgos clave: máximo 6 viñetas, cada una con su cita archivo:línea.\n"
    "3) Riesgos: una línea por riesgo, ordenados por severidad.\n"
    "4) Qué falta decidir o verificar: 2 a 4 puntos.\n"
    "No repitas el análisis, no incluyas diagramas ni código nuevo; donde falte el dato escribe "
    "\"sin evidencia\". No termines ofreciendo más trabajo (nada de \"¿Quieres que…?\")."
)
# Exploratorio (architecture solo): la misma forma, sobre lo que el agente leyó en esta corrida.
EXPLORE_BLOCK = _DECISION_SHAPE
# Decisión (último paso de un plan de varios agentes): además, nada que no esté en el paso previo.
DECISION_BLOCK = (
    _DECISION_SHAPE
    + "\nEl reporte no añade datos, identificadores ni cifras que no estén en el resultado del paso "
    "previo; conserva sus citas archivo:línea."
)

REPORT_BLOCK = (
    "---\n"
    "Formato de reporte: redacta SOLO sobre el resultado de los pasos previos de este plan y, si "
    "no lo hay, sobre lo que leas con tus tools en esta corrida. No incluyas bloques de código, "
    "identificadores ni cifras (fechas, ids, KCV, plazos) que no aparezcan ahí; donde falte el dato "
    "escribe \"sin evidencia\". Conserva las citas archivo:línea que ya vengan. Máximo unas 60 "
    "líneas. No termines ofreciendo más trabajo (nada de \"¿Quieres que…?\")."
)

VOICE_CERCANA = "cercana"
VOICE_DIRECTA = "directa"
VOICES = (VOICE_CERCANA, VOICE_DIRECTA)
# Perfiles que llevan voz; el completo (--completo, --debug, "en detalle") nunca.
VOICE_PROFILES = (PROFILE_BRIEF, PROFILE_EXPLORE, PROFILE_DECISION, PROFILE_REPORT)
_VOICE_COMMON = (
    "Marca cada riesgo con 🔴 (alto), 🟠 (medio) o 🟡 (bajo) según severidad. Termina con UNA "
    "recomendación concreta, apoyada en un hallazgo con su cita archivo:línea, en lugar de ofrecer "
    "más trabajo. No añadas hechos nuevos: la voz solo encuadra lo que ya está en la respuesta."
)
VOICE_BLOCKS = {
    VOICE_CERCANA: (
        "Voz cercana: abre con 1 a 3 líneas en lenguaje natural que digan lo más importante para "
        "quien pregunta; después sigue la estructura indicada. " + _VOICE_COMMON
    ),
    VOICE_DIRECTA: (
        "Voz directa: sin apertura, ve a la estructura indicada. " + _VOICE_COMMON
    ),
}

BOUNDED_NOTE = "_Respuesta acotada; pide 'en detalle' para ampliar._"
BOUNDED_PROFILES = (PROFILE_BRIEF, PROFILE_EXPLORE, PROFILE_DECISION)

# Lista cerrada: solo estas aperturas cuentan como "oferta de más trabajo".
OFFER_PATTERNS = ("¿quieres", "¿te gustaría", "¿deseas", "¿necesitas", "¿te sirve")

_FULL_RE = re.compile(r"\ben detalle\b|\bcompleto\b", re.IGNORECASE)
_LINE_DECOR_RE = re.compile(r"^[\s>*_\-–•]+|[\s*_]+$")


def wants_full(request: str) -> bool:
    """El dev pide el modo completo en el propio pedido."""
    return bool(_FULL_RE.search(request or ""))


def profile_for(
    router_name: str | None,
    agent: str,
    force_full: bool = False,
    plan_agents: list[str] | None = None,
    last_of_plan: bool = False,
) -> str:
    """`plan_agents`: agentes del plan completo. El último paso de un plan de 2+ agentes recibe
    `decision` (también si es report_agent); el exploratorio exige router llm y plan de solo
    architecture_agent. "en detalle", --completo y --debug (force_full) dejan todo en completo."""
    multi_last = bool(plan_agents) and len(plan_agents) > 1 and last_of_plan
    if agent in REPORT_AGENTS:
        return PROFILE_DECISION if multi_last and not force_full else PROFILE_REPORT
    if force_full:
        return PROFILE_FULL
    if router_name in BRIEF_ROUTERS:
        return PROFILE_BRIEF
    if multi_last:
        return PROFILE_DECISION
    if router_name in EXPLORE_ROUTERS and plan_agents and set(plan_agents) <= set(EXPLORE_AGENTS):
        return PROFILE_EXPLORE
    return PROFILE_FULL


def voice_block(voice: str | None, profile: str) -> str:
    """Bloque de voz para el mensaje user; vacío sin voz o en el perfil completo."""
    if not voice or profile not in VOICE_PROFILES:
        return ""
    return VOICE_BLOCKS.get(str(voice).strip().lower(), "")


def user_block(profile: str, voice: str | None = None) -> str:
    """Texto que se añade al mensaje user; vacío en el perfil completo."""
    if profile == PROFILE_BRIEF:
        block = BRIEF_BLOCK
    elif profile == PROFILE_EXPLORE:
        block = EXPLORE_BLOCK
    elif profile == PROFILE_DECISION:
        block = DECISION_BLOCK
    elif profile == PROFILE_REPORT:
        block = REPORT_BLOCK
    else:
        return ""
    extra = voice_block(voice, profile)
    return f"{block}\n{extra}" if extra else block


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
    """Solo para mostrar: avisa que la respuesta fue acotada (perfil breve o exploratorio)."""
    if profile not in BOUNDED_PROFILES or not text:
        return text
    return f"{text.rstrip()}\n\n{BOUNDED_NOTE}"
