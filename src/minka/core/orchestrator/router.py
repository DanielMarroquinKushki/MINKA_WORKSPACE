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
from minka.core.tools.git import git_tools

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
# Pregunta de información sobre el proyecto: va a architecture aunque el planner no dé plan.
_INFO_WORDS = (
    r"\b(donde|como|que (hace|hacen|es|son|significa|pasa|ocurre)|por que|cual(es)?|cuant[oa]s?|"
    r"quien|lista|explica|muestra|describe|flujo|riesgos?|dependencias|arquitectura)\b"
)
_PRONOUNS = {"eso", "esto", "aquello", "ello", "lo", "entonces", "ahora", "ya", "y", "o", "de", "el", "la"}
# Forma de error, no la palabra suelta: "configura el timeout" no es un fallo; "falla por
# timeout", "da timeout" o "timed out" sí.
_FAILURE = (
    r"(stack ?trace|traceback|panic|\berror(es)?\b|excepci[oó]n|exception|\bfalla[n]?\b|"
    r"\bfall[oó]\b|no funciona|nil pointer|segfault|\b(por|da|dio|de) timeout\b|timed out|"
    r"time out)"
)
# Encadenado: el pedido pide más de una cosa → planner (puede devolver varios pasos).
_CHAIN_WORDS = r"\b(y luego|luego|despues|ademas|y tambien|y despues|y entonces|y a continuacion)\b"
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
    notices: list[str] = field(default_factory=list)
    # Del planner LLM: por qué no hubo plan (kind/class/message), su reasoning y reintentos.
    planner_error: dict | None = None
    reasoning: str | None = None
    retries: int = 0


ASK_AMBIGUOUS = (
    "Tu pedido es ambiguo. Dime qué quieres (analizar, revisar un diff, depurar un fallo, "
    "implementar un cambio o un reporte) y sobre qué código (ruta, archivo, función o repo)."
)
ASK_NO_PLAN = (
    "No identifiqué una tarea en tu pedido. Dime qué quieres (analizar, revisar un diff, "
    "depurar un fallo, implementar un cambio o un reporte) y sobre qué código."
)


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


def is_information_question(request: str) -> bool:
    """Interrogativa o con dónde/cómo/qué/por qué/lista/explica/muestra… y con algún
    sustantivo real: "¿qué opinas de eso?" no lo es, "¿dónde se resuelve el token?" sí."""
    low = _normalize(request)
    m = re.search(_INFO_WORDS, low)
    if not m:
        return False
    rest = low[: m.start()] + low[m.end() :]
    content = [w for w in rest.split() if len(w) >= 4 and w not in _PRONOUNS]
    return bool(content)


def is_simple_info_question(request: str) -> bool:
    """Una sola pregunta de información, sin verbo de escritura, fallo ni encadenado:
    se puede enrutar a architecture sin planner."""
    low = _normalize(request)
    if not is_information_question(request):
        return False
    if has_write_verb(request) or has_failure(request) or re.search(_CHAIN_WORDS, low):
        return False
    return request.count("?") <= 1 and request.count("¿") <= 1


def is_ambiguous(request: str, repos: dict | None = None) -> bool:
    """Ambiguo si: verbo de escritura o fallo SIN código citado ni pregunta de información
    ("arregla el bug"); o sin verbo, pregunta, código, fallo, diff ni reporte y ≤2 palabras
    o arranque de seguimiento."""
    low = _normalize(request)
    if not low:
        return True
    code = has_code_reference(request, repos)
    if (
        (has_write_verb(request) or has_failure(request))
        and not code
        and not is_information_question(request)
    ):
        return True
    if (
        _keyword_hit(request)
        or re.search(_QUERY_WORDS, low)
        or code
        or has_diff_reference(request)
        or asks_report(request)
    ):
        return False
    return len(low.split()) <= 2 or re.match(_FOLLOWUP_START, low) is not None


# -- Capa (a): ¿hay algo que revisar antes de lanzar pr_review? -------------------------
_FILE_EXT = r"\.(?:go|py|ts|js|java|rs|rb|yml|yaml|json|toml|md|sql|proto|sh)\b"
_REF_WORD = r"\b(?:rama|branch|commit|tag|pr|pull request|merge request)\s+#?([\w./-]{2,})"
_HEX_REF = r"\b[0-9a-f]{7,40}\b"
_BRANCH_LIKE = r"\b[\w.-]+/[\w./-]+"


def cited_ref(request: str) -> str | None:
    """Rama, commit o PR citados en el pedido: eso es lo que pr_review debe revisar."""
    m = re.search(_REF_WORD, request, re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(_HEX_REF, request.lower())
    if m:
        return m.group(0)
    for m in re.finditer(_BRANCH_LIKE, request):
        if not re.search(_FILE_EXT, m.group(0)):
            return m.group(0)
    return None


def _target_repo(request: str, repos: dict | None) -> tuple[str, str] | None:
    """(alias, ruta) del repo citado, o el único configurado; None si no se sabe."""
    configured = [(a, p) for a, p in (repos or {}).items() if p]
    low = _normalize(request)
    hits = [(a, p) for a, p in configured if re.search(rf"\b{re.escape(_normalize(a))}\b", low)]
    if len(hits) == 1:
        return hits[0]
    if not hits and len(configured) == 1:
        return configured[0]
    return None


def no_changes_reply(request: str, repos: dict | None) -> str | None:
    """Sin rama/commit citados y con el repo limpio (solo lectura: status/diff), no hay
    nada que revisar: respuesta a costo 0 en vez de lanzar pr_review."""
    if cited_ref(request):
        return None
    target = _target_repo(request, repos)
    if target is None:
        return None
    alias, path = target
    if git_tools.has_pending_changes(path) is not False:
        return None
    return (
        f"No hay cambios que revisar en {alias}: árbol limpio y sin diff contra HEAD. "
        "Cita una rama o commit si quieres revisar algo ya integrado."
    )


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
    # (b) "revisa este diff" sin rama/commit citados y con el repo limpio: $0, sin planner.
    if has_diff_reference(request):
        reply = no_changes_reply(request, repos)
        if reply:
            return RouteDecision(
                "no_changes", "sin cambios que revisar", reply=reply, confidence="alta"
            )
    if is_ambiguous(request, repos):
        return _fallback_decision(
            session_turns, "ambiguous", "ambiguo: precisa el pedido", ASK_AMBIGUOUS
        )
    # Pregunta de información simple → architecture directo, sin planner (PLANNER_SKIP_INFO).
    if getattr(config, "PLANNER_SKIP_INFO", True) and is_simple_info_question(request):
        return RouteDecision(
            "info_direct",
            "consulta de información, directo",
            agents=["architecture_agent"],
            confidence="alta",
        )
    return None


def _fallback_decision(
    session_turns: list[dict],
    router: str,
    reason: str,
    ask_text: str,
    inherit_notices: list[str] | None = None,
    **extra,
) -> RouteDecision:
    """Capa 4: con turnos previos hereda su agente; sin ellos, una línea pidiendo precisar."""
    previous = next((t["agents"] for t in reversed(session_turns) if t.get("agents")), None)
    if previous:
        return RouteDecision(
            "inherit",
            "hereda turno previo",
            agents=[previous[-1]],
            notices=list(inherit_notices or []),
            **extra,
        )
    return RouteDecision(router, reason, reply=ask_text, confidence="baja", **extra)


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
    result = plan_with_router(request, use_llm=use_llm, context=context)
    extra = {
        "planner_error": result.planner_error,
        "reasoning": result.reasoning,
        "retries": result.retries,
    }
    if result.router == "no_plan":
        # El LLM respondió sin pasos o con algo que no valida. Una pregunta de
        # información va igual a architecture (solo lectura); lo demás es capa 4.
        # Nunca keywords.
        kind = (result.planner_error or {}).get("kind")
        why = "planner: respuesta inválida" if kind == "validation" else "planner: sin plan"
        if is_information_question(request):
            return RouteDecision(
                "no_plan",
                "pregunta de información",
                agents=["architecture_agent"],
                notices=[f"{why} → architecture"],
                **extra,
            )
        return _fallback_decision(
            session_turns, "no_plan", why, ASK_NO_PLAN, inherit_notices=[why], **extra
        )
    steps = result.steps
    keyword_agent = _keyword_hit(request)
    first = steps[0]["agent"] if steps else None
    if result.router == "llm":
        confidence = "alta" if keyword_agent and first == keyword_agent else "media"
    else:
        confidence = "baja" if result.planner_error else "media"
    steps, notices = apply_hard_rules(steps, request, bool(session_turns), repos)
    if notices:
        confidence = "baja"
    if result.planner_error:
        notices.insert(0, f"planner LLM: {result.planner_error['kind']}, keywords")
    agents = [s["agent"] for s in steps]
    reason = REASONS.get(agents[0], "plan") if len(agents) == 1 else f"plan de {len(agents)} pasos"
    return RouteDecision(
        result.router,
        reason,
        agents=agents,
        steps=steps,
        confidence=confidence,
        notices=notices,
        **extra,
    )


def describe(decision: RouteDecision) -> str:
    """Una línea para el dev: `→ architecture (consulta de código) · aviso`.

    Con DEBUG añade el error del planner (clase y mensaje) y su reasoning.
    """
    head = " → ".join(_short(a) for a in decision.agents) or "sin agente"
    line = f"→ {head} ({decision.reason})"
    if decision.notices:
        line += " · " + " · ".join(decision.notices)
    if getattr(config, "DEBUG", False):
        if decision.planner_error:
            err = decision.planner_error
            detail = " ".join(x for x in (err.get("kind"), err.get("class")) if x)
            line += f" · planner_error: {detail}: {err.get('message', '')}"
        if decision.reasoning:
            line += f" · reasoning: {decision.reasoning[:120]}"
    return line


def read_only_message(agents: list[str]) -> str:
    needed = ", ".join(_short(a) for a in agents if a not in READONLY_AGENTS)
    return (
        f"Modo solo lectura: el pedido requiere {needed}, que escribe o depura con el "
        f"ApprovalGate interactivo. Úsalo en el modo conversacional de {APP_NAME}."
    )
