"""router — decide qué agente atiende el pedido ANTES de gastar tokens.

Capas, en orden:
1. Respuestas deterministas a costo 0: saludo, estado de la sesión, meta sobre la app.
2. Planner (LLM con fallback por keywords) SOLO sobre el pedido actual; el historial
   va aparte, rotulado "contexto, no enrutar", solo para resolver seguimientos.
3. Reglas duras post-planner: cada agente exige su evidencia en el pedido.
4. Ambigüedad: con turnos previos hereda el agente; sin ellos pide precisar.

main.py antepone el historial al pedido separado por CURRENT_REQUEST_MARKER; aquí se
separa y el historial nunca se enruta.
"""

import re
import unicodedata
from dataclasses import dataclass, field

from minka._app import APP_NAME
from minka.config_loader import config
from minka.core.orchestrator.planner import _ROUTING, plan_with_router

CURRENT_REQUEST_MARKER = "Pedido actual del dev:"

# Agentes que no escriben ni pasan por el ApprovalGate (modo --read-only / bridge).
READONLY_AGENTS = ("architecture_agent", "report_agent", "pr_review_agent")

REASONS = {
    "architecture_agent": "consulta de código",
    "development_agent": "cambio de código",
    "debug_agent": "diagnóstico de fallo",
    "pr_review_agent": "revisión de cambios",
    "report_agent": "reporte",
    "meta_agent": "evaluación de la sesión",
}

# Frases (no palabras sueltas): "estado" solo no es una pregunta de estado.
_STATE_PHRASES = (
    r"en que quedamos",
    r"lo ultimo que vimos",
    r"que hicimos",
    r"que hemos hecho",
    r"donde (nos )?quedamos",
    r"que estabamos haciendo",
    r"resumen de (la|esta) sesion",
)
_META_PHRASES = (
    r"^(que|quien) eres",
    r"que (puedes|sabes) hacer",
    r"como (funcionas|te uso|se usa)",
    r"que agentes (tienes|hay)",
    r"para que sirves",
    r"^(ayuda|help)$",
    r"que comandos",
    rf"(que es|como funciona) {APP_NAME}",
)
_QUERY_WORDS = (
    r"\b(donde|como|que hace[n]?|cual(es)?|por que|flujo|riesgos?|explica|muestra|lista|"
    r"analiza|revisa|describe|compara|busca|cuant[oa]s?|quien)\b"
)
_FOLLOWUP_START = r"^(y|entonces|ahora|tambien|lo mismo|eso|esto|otra vez|de nuevo|igual|el otro|la otra)\b"
_FAILURE = (
    r"(stack ?trace|traceback|panic|\berror\b|excepcion|exception|\bfalla\b|\bfallo\b|"
    r"no funciona|timeout|nil pointer|segfault|goroutine|\b5\d\d\b)"
)
_DIFF = r"\b(diff|pull request|pr|mr|rama|branch|commit|merge|cambios propuestos)\b"
_REPORT = r"\b(reporte|informe|documenta|documentar|documento|minuta|resumen ejecutivo)\b"
_WRITE_VERBS = sorted(
    {kw for agent, kws in _ROUTING if agent == "development_agent" for kw in kws}
    | {
        "modifica", "modificar", "cambia", "cambiar", "anade", "añade", "agregar",
        "elimina", "eliminar", "borra", "borrar", "renombra", "mueve", "actualiza",
        "actualizar", "arregla", "arreglar",
    }
)
# Sobre el texto original (camelCase se pierde al normalizar).
_CODE_REF = (
    r"(?=\S*[A-Za-z])[\w.-]+/[\w./-]+",  # ruta o rama
    r"\b\w+\.(go|py|ts|js|java|rs|rb|yml|yaml|json|toml|md|sql|proto|sh)\b",  # archivo
    r"\b\w+\(\)",  # función()
    r"`[^`]+`",  # código inline
    r"\b[a-z]+_[a-z_]+\b",  # snake_case
    r"\b[a-z]+[A-Z]\w+\b",  # camelCase
    r"\b(funci[oó]n|archivo|m[eé]todo|clase|m[oó]dulo|paquete|handler|endpoint|lambda|struct|"
    r"interfaz|servicio|tabla|repo|repositorio)\s+[\w./-]{3,}",
)


@dataclass
class RouteDecision:
    router: str
    reason: str
    agents: list[str] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)
    reply: str | None = None
    confidence: str = "media"
    llm_failed: bool = False
    notices: list[str] = field(default_factory=list)


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^\w\s]", " ", text)
    return " ".join(text.split())


def split_request(task: str) -> tuple[str, str]:
    """(historial, pedido actual). Sin marcador, todo es el pedido."""
    if CURRENT_REQUEST_MARKER in task:
        context, request = task.rsplit(CURRENT_REQUEST_MARKER, 1)
        return context.strip(), request.strip()
    return "", task.strip()


def _short(agent: str) -> str:
    return agent.removesuffix("_agent")


# -- Evidencia en el pedido ------------------------------------------------------
def has_code_reference(request: str, repos: dict | None = None) -> bool:
    if any(re.search(p, request) for p in _CODE_REF):
        return True
    low = _normalize(request)
    return any(re.search(rf"\b{re.escape(_normalize(alias))}\b", low) for alias in (repos or {}))


def has_failure(request: str) -> bool:
    return re.search(_FAILURE, _normalize(request)) is not None


def has_write_verb(request: str) -> bool:
    low = _normalize(request)
    return any(re.search(rf"\b{re.escape(_normalize(v))}\b", low) for v in _WRITE_VERBS)


def has_diff_reference(request: str) -> bool:
    return re.search(_DIFF, _normalize(request)) is not None


def asks_report(request: str) -> bool:
    return re.search(_REPORT, _normalize(request)) is not None


def _keyword_hit(request: str) -> str | None:
    low = request.lower()
    for agent, keywords in _ROUTING:
        if any(kw in low for kw in keywords):
            return agent
    return None


def is_ambiguous(request: str, repos: dict | None = None) -> bool:
    """Sin verbo, pregunta, código, fallo, diff ni reporte: ≤2 palabras o arranque de seguimiento."""
    low = _normalize(request)
    if not low:
        return True
    if (
        _keyword_hit(request)
        or re.search(_QUERY_WORDS, low)
        or has_code_reference(request, repos)
        or has_failure(request)
        or has_diff_reference(request)
        or asks_report(request)
    ):
        return False
    return len(low.split()) <= 2 or re.match(_FOLLOWUP_START, low) is not None


# -- Capa 1: respuestas deterministas ------------------------------------------
def small_talk_reply(request: str, agent_names: tuple[str, ...]) -> str | None:
    """Texto fijo si el pedido es solo un saludo/agradecimiento; None si no."""
    text = _normalize(request)
    if not text or len(text.split()) > getattr(config, "SMALL_TALK_MAX_WORDS", 4):
        return None
    patterns = getattr(config, "SMALL_TALK_PATTERNS", [])
    if not any(re.fullmatch(p, text) for p in patterns):
        return None
    agents = ", ".join(_short(n) for n in agent_names)
    return (
        f"¡Hola! Soy {APP_NAME}. Pídeme una tarea concreta (analizar, revisar, "
        f"depurar, documentar o desarrollar). Agentes disponibles: {agents}."
    )


def state_reply(request: str, session_turns: list[dict]) -> str | None:
    text = _normalize(request)
    if not any(re.search(p, text) for p in _STATE_PHRASES):
        return None
    head = f"No tengo contexto del proyecto cargado (eso vive fuera de {APP_NAME})."
    if not session_turns:
        return f"{head} En esta sesión no hay turnos todavía: pídeme una tarea concreta."
    lines = [f"{head} En esta sesión:"]
    for i, turn in enumerate(session_turns, 1):
        agents = " → ".join(_short(a) for a in turn.get("agents", [])) or "sin agente"
        lines.append(f"  {i}. {turn.get('request', '')[:80]} → {agents}")
    return "\n".join(lines)


def meta_reply(request: str, agent_names: tuple[str, ...]) -> str | None:
    text = _normalize(request)
    if not any(re.search(p, text) for p in _META_PHRASES):
        return None
    agents = ", ".join(_short(n) for n in agent_names)
    return (
        f"Soy {APP_NAME}: oriento cada pedido al agente adecuado y lo ejecuto sobre los repos "
        f"configurados. Agentes: {agents}. Comandos: /status, /clear, salir; "
        f"`{APP_NAME} stats` muestra tokens y costo; `--agent` fuerza un agente; "
        f"`--quiet` silencia la línea de métricas."
    )


def local_decision(
    request: str,
    session_turns: list[dict],
    agent_names: tuple[str, ...],
    repos: dict | None = None,
) -> RouteDecision | None:
    """Capas 1 y 4. None si el pedido debe ir al planner."""
    reply = small_talk_reply(request, agent_names)
    if reply:
        return RouteDecision("small_talk", "saludo", reply=reply, confidence="alta")
    reply = state_reply(request, session_turns)
    if reply:
        return RouteDecision("state", "estado de la sesión", reply=reply, confidence="alta")
    reply = meta_reply(request, agent_names)
    if reply:
        return RouteDecision("meta", f"meta sobre {APP_NAME}", reply=reply, confidence="alta")
    if is_ambiguous(request, repos):
        previous = next((t["agents"] for t in reversed(session_turns) if t.get("agents")), None)
        if previous:
            return RouteDecision("inherit", "hereda turno previo", agents=[previous[-1]])
        return RouteDecision(
            "ambiguous",
            "ambiguo: precisa el pedido",
            reply=(
                "Tu pedido es ambiguo. Dime qué quieres (analizar, revisar un diff, depurar un "
                "fallo, implementar un cambio o un reporte) y sobre qué código (ruta, archivo, "
                "función o repo)."
            ),
            confidence="baja",
        )
    return None


# -- Capa 3: reglas duras ------------------------------------------------------------
def apply_hard_rules(
    steps: list[dict], request: str, has_prev_result: bool, repos: dict | None = None
) -> tuple[list[dict], list[str]]:
    """Cada agente exige su evidencia en el pedido; si falta, el paso pasa a architecture."""
    rules = {
        "development_agent": (
            lambda: has_write_verb(request) and has_code_reference(request, repos),
            "development exige verbo de escritura y código citado",
        ),
        "debug_agent": (
            lambda: has_failure(request) or has_code_reference(request, repos),
            "debug exige un fallo o código citado",
        ),
        "pr_review_agent": (
            lambda: has_diff_reference(request),
            "pr_review exige diff, PR o rama",
        ),
        "report_agent": (
            lambda: asks_report(request) or has_prev_result,
            "report exige pedir un reporte o un resultado previo",
        ),
    }
    out: list[dict] = []
    notices: list[str] = []
    for step in steps:
        agent = step["agent"]
        rule = rules.get(agent)
        if rule and not rule[0]():
            notices.append(f"regla: {rule[1]} → architecture")
            agent = "architecture_agent"
        if out and agent == "architecture_agent" and out[-1]["agent"] == agent:
            continue  # dos análisis seguidos sobre el mismo pedido no suman
        out.append({**step, "agent": agent})
    return out, notices


# -- Capas 2 + 3 -------------------------------------------------------------------
def plan_decision(
    request: str,
    context: str,
    use_llm: bool,
    session_turns: list[dict],
    repos: dict | None = None,
) -> RouteDecision:
    steps, router, llm_failed = plan_with_router(request, use_llm=use_llm, context=context)
    keyword_agent = _keyword_hit(request)
    first = steps[0]["agent"] if steps else None
    if router == "llm":
        confidence = "alta" if keyword_agent and first == keyword_agent else "media"
    else:
        confidence = "baja" if llm_failed else "media"
    steps, notices = apply_hard_rules(steps, request, bool(session_turns), repos)
    if notices:
        confidence = "baja"
    if llm_failed:
        notices.insert(0, "planner LLM falló, keywords")
    agents = [s["agent"] for s in steps]
    reason = REASONS.get(agents[0], "plan") if len(agents) == 1 else f"plan de {len(agents)} pasos"
    return RouteDecision(router, reason, agents, steps, None, confidence, llm_failed, notices)


def describe(decision: RouteDecision) -> str:
    """Una línea para el dev: `→ architecture (consulta de código) · aviso`."""
    head = " → ".join(_short(a) for a in decision.agents) or "sin agente"
    line = f"→ {head} ({decision.reason})"
    if decision.notices:
        line += " · " + " · ".join(decision.notices)
    return line


def read_only_message(agents: list[str]) -> str:
    needed = ", ".join(_short(a) for a in agents if a not in READONLY_AGENTS)
    return (
        f"Modo solo lectura: el pedido requiere {needed}, que escribe o depura con el "
        f"ApprovalGate interactivo. Úsalo en el modo conversacional de {APP_NAME}."
    )
