"""
planner — descompone el pedido del dev en un plan de steps (agente + tarea).

Dos estrategias:
1. LLM: pide a Claude un plan JSON (usa ORCHESTRATOR_SYSTEM_PROMPT).
2. Fallback por keywords: routing determinista si el LLM no está disponible o
   devuelve algo no parseable. Cubre los patrones del PASO 8 del prompt.

El fallback también sirve para pruebas offline (sin API key).
"""

from config_loader import config
from core.prompt_loader import load_system_prompt

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


def _llm_plan(task: str) -> list[dict] | None:
    """Intenta obtener un plan del LLM vía salida estructurada. None si falla."""
    try:
        # Imports perezosos: el routing por keywords no necesita cliente LLM.
        # Usa el mismo backend (Bedrock/API directa) que los agentes.
        from core.agents.base_agent import build_llm_client, resolve_model_id
        from core.schemas import Plan
        from core.structured import call_structured

        client = build_llm_client()
        model = resolve_model_id(
            config.MODELS.get("planner", getattr(config, "DEFAULT_MODEL", config.ANTHROPIC_MODEL))
        )
        plan = call_structured(
            client,
            model,
            system=load_system_prompt("orchestrator"),
            messages=[{"role": "user", "content": task}],
            schema_model=Plan,
            tool_name="submit_plan",
            tool_description="Devuelve el plan de ejecución: agentes y tareas en orden.",
        )
        if plan is None or not plan.steps:
            return None
        return [{"agent": s.agent, "task": s.task} for s in plan.steps]
    except Exception:  # noqa: BLE001 — cualquier fallo cae al keyword plan
        return None


def make_plan(task: str, use_llm: bool = True) -> list[dict]:
    """
    Devuelve una lista de steps [{"agent": ..., "task": ...}].
    Intenta LLM; si falla, usa routing por keywords.
    """
    if use_llm:
        plan = _llm_plan(task)
        if plan:
            return plan
    return _keyword_plan(task)
