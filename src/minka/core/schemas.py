"""
schemas — modelos Pydantic para las salidas estructuradas del LLM.

Se usan con core.structured.call_structured para que el modelo responda con una
forma validada en vez de texto libre parseado con regex/strings.
"""

from typing import Literal

from pydantic import BaseModel, Field

# Agentes válidos a los que el planner puede rutear (coincide con el registry
# del orchestrator y con planner._ROUTING). Si el LLM inventa otro, la validación
# falla y el caller cae a su fallback.
AgentName = Literal[
    "architecture_agent",
    "development_agent",
    "pr_review_agent",
    "debug_agent",
    "report_agent",
    "meta_agent",
]


class PlanStep(BaseModel):
    """Un paso del plan: qué agente ejecuta qué tarea."""

    agent: AgentName = Field(description="Agente que ejecuta este paso.")
    task: str = Field(description="Tarea concreta para el agente, en español.")


class Plan(BaseModel):
    """Plan de ejecución: lista ordenada de pasos."""

    # minItems=1 es lo que corta el steps=[]: con la pista sola el planner devolvía vacío en
    # 86% de las preguntas de información; con min_length=1, 0% (36 llamadas, 2026-11-03).
    # Si el modelo igual manda [] falla la validación y el router aplica su red de seguridad.
    steps: list[PlanStep] = Field(
        min_length=1,
        description="Al menos un paso; si no es una tarea concreta, architecture_agent.",
    )
    # Opcional: el prompt lo pide ("reasoning") y antes se perdía. Solo métricas y --debug.
    reasoning: str | None = Field(
        default=None, description="Por qué se eligieron estos agentes; explica en máximo 6 palabras."
    )


class ReviewDecision(BaseModel):
    """Decisión de routing del pr_review sobre un cambio (fix-loop Nivel 5)."""

    routing_decision: Literal["aprobar", "corregir", "escalar"] = Field(
        description=(
            "aprobar = el cambio está bien; "
            "corregir = hay hallazgos bloqueantes a arreglar; "
            "escalar = requiere revisión humana."
        )
    )
    blockers: list[str] = Field(
        default_factory=list,
        description="Hallazgos bloqueantes a corregir (solo si routing_decision='corregir').",
    )


class Finding(BaseModel):
    """Hallazgo de una lente de revisión 4R (RDD). La dimensión la asigna el caller."""

    status: Literal["CUMPLE", "REVISAR", "BLOQUEANTE"] = Field(
        description=(
            "CUMPLE = sin problemas; REVISAR = mejorable, no bloqueante; "
            "BLOQUEANTE = debe corregirse antes de aprobar."
        )
    )
    evidence: str = Field(
        default="", description="archivo:línea → fragmento exacto que sustenta el hallazgo."
    )
    suggestion: str = Field(default="", description="Qué cambiar (vacío si CUMPLE).")
