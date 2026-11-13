"""
planner — descompone el pedido del dev en un plan de steps (agente + tarea).

Dos estrategias:
1. LLM: pide a Claude un plan JSON (usa ORCHESTRATOR_SYSTEM_PROMPT).
2. Fallback por keywords: routing determinista si el LLM no está disponible o
   devuelve algo no parseable. Cubre los patrones del PASO 8 del prompt.

El fallback también sirve para pruebas offline (sin API key).
"""

from dataclasses import dataclass

from minka.config_loader import config
from minka.core.prompt_loader import load_system_prompt
from minka.core.structured import StructuredError


@dataclass
class LLMPlan:
    """Respuesta cruda del planner LLM: pasos o error tipado (ver StructuredError.kind)."""

    steps: list[dict] | None
    error: StructuredError | None = None
    reasoning: str | None = None
    retries: int = 0
    usage: dict | None = None  # tokens de la(s) llamada(s) al planner


@dataclass
class PlanResult:
    """Lo que el router necesita: pasos, quién decidió y por qué falló el LLM, si falló.

    router: "llm" | "keyword" | "no_plan" (el LLM respondió sin pasos o con una
    respuesta que no valida: el router aplica la red de seguridad, nunca keywords).
    """

    steps: list[dict]
    router: str
    planner_error: dict | None = None
    reasoning: str | None = None
    retries: int = 0
    usage: dict | None = None  # tokens del planner LLM (None con keywords)


# Routing por intención (keywords). Orden importa: se evalúa de arriba a abajo.
_ROUTING = [
    (
        "development_agent",
        [
            "corrige",
            "corregir",
            "implementa",
            "implementar",
            "refactoriza",
            "refactorizar",
            "crea lambda",
            "escribe",
            "escribir",
            "arregla",
            "agrega",
        ],
    ),
    (
        "pr_review_agent",
        [
            "revisa pr",
            "revisar pr",
            "valida cambio",
            "valida el diff",
            "compara versiones",
            "review",
            "revisa el diff",
        ],
    ),
    (
        "debug_agent",
        [
            "error",
            "bug",
            "por qué falla",
            "porque falla",
            "falla",
            "traza",
            "debug",
            "no funciona",
            "excepción",
            "panic",
        ],
    ),
    (
        "report_agent",
        [
            "genera reporte",
            "generar reporte",
            "documenta",
            "documentar",
            "crea el informe",
            "informe",
            "reporte de",
        ],
    ),
    (
        "architecture_agent",
        [
            "analiza",
            "analizar",
            "revisa",
            "detecta riesgo",
            "riesgos",
            "dependencias",
            "patrones",
            "interfaces",
            "arquitectura",
            "versiones",
        ],
    ),
]


def _keyword_plan(task: str) -> list[dict]:
    low = task.lower()
    for agent, keywords in _ROUTING:
        if any(kw in low for kw in keywords):
            return [{"agent": agent, "task": task}]
    # Default: análisis de arquitectura (lo más seguro, solo lectura).
    return [{"agent": "architecture_agent", "task": task}]


# El historial de la sesión viaja en un bloque aparte: sirve para resolver
# seguimientos ("¿y en el otro repo?"), nunca para decidir el agente. El rótulo
# no debe sonar a "no hagas nada": con "contexto, no enrutar" el modelo devolvía
# steps vacíos (medido: 1 llamada real, planner_error=empty_plan).
CONTEXT_LABEL = (
    "Turnos previos de la sesión. Referencia solo para resolver seguimientos. "
    "Enruta SOLO el pedido actual y devuelve siempre al menos un paso."
)


def _planner_messages(task: str, context: str) -> list[dict]:
    if not context:
        return [{"role": "user", "content": task}]
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": f"{CONTEXT_LABEL}\n{context}"},
                {"type": "text", "text": f"Pedido actual:\n{task}"},
            ],
        }
    ]


def _llm_plan(task: str, context: str = "") -> LLMPlan:
    """Plan del LLM vía salida estructurada, o el error tipado de por qué no hubo.

    `task` es SOLO el pedido actual; `context` el historial rotulado aparte.
    Reintenta una vez los errores transitorios (red, cuota, 5xx).
    """
    try:
        # Imports perezosos: el routing por keywords no necesita cliente LLM.
        # Usa el mismo backend (Bedrock/API directa) que los agentes.
        from minka.core.agents.base_agent import build_llm_client, resolve_model_id
        from minka.core.schemas import Plan
        from minka.core.structured import call_structured_result

        client = build_llm_client()
        model = resolve_model_id(
            config.MODELS.get("planner", getattr(config, "DEFAULT_MODEL", config.ANTHROPIC_MODEL))
        )
        plan, error, retries, usage = call_structured_result(
            client,
            model,
            system=load_system_prompt("orchestrator"),
            messages=_planner_messages(task, context),
            schema_model=Plan,
            tool_name="submit_plan",
            tool_description="Devuelve el plan de ejecución: agentes y tareas en orden.",
            retry_transient=True,
            retry_validation=True,
        )
    except Exception as exc:  # noqa: BLE001 — p.ej. sin backend LLM configurado
        return LLMPlan(None, StructuredError("api", type(exc).__name__, str(exc)[:200]))
    if error is not None:
        return LLMPlan(None, error, None, retries, usage)
    reasoning = getattr(plan, "reasoning", None)
    if not plan.steps:
        empty = StructuredError("empty_plan", "", "el planner no devolvió pasos")
        return LLMPlan(None, empty, reasoning, retries, usage)
    steps = [{"agent": s.agent, "task": s.task} for s in plan.steps]
    return LLMPlan(steps, None, reasoning, retries, usage)


def plan_with_router(task: str, use_llm: bool = True, context: str = "") -> PlanResult:
    """Intenta LLM sobre el pedido actual con el historial aparte; sin LLM (o si el
    LLM falla) usa keywords solo sobre `task`. Si el LLM respondió sin pasos
    (empty_plan) NO cae a keywords: devuelve router="no_plan" para la capa 4."""
    if not use_llm:
        return PlanResult(_keyword_plan(task), "keyword")
    res = _llm_plan(task, context)
    if res.steps:
        return PlanResult(res.steps, "llm", None, res.reasoning, res.retries, res.usage)
    error = res.error or StructuredError("empty_plan", "", "el planner no devolvió pasos")
    if error.kind in ("empty_plan", "validation"):
        return PlanResult([], "no_plan", error.as_dict(), res.reasoning, res.retries, res.usage)
    return PlanResult(_keyword_plan(task), "keyword", error.as_dict(), res.reasoning, res.retries, res.usage)


def make_plan(task: str, use_llm: bool = True) -> list[dict]:
    """Solo los steps (keywords si el LLM no dio plan); ver plan_with_router."""
    return plan_with_router(task, use_llm).steps or _keyword_plan(task)
