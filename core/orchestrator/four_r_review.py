"""
four_r_review.py — Las 4R de RDD adaptadas al dev-agent (Nivel 5+).

Corre 4 lentes de revisión (Riesgo/Legibilidad/Robustez/Resiliencia) sobre un
candidato, cada una vía call_structured (structured outputs, SIN regex). NO es
paralelo: secuencial, mismo criterio que el core de Gentle AI. Se usa una vez
por candidato dentro del fix-loop cuando EVALUATOR_ENABLED=True.

Cada lente devuelve un Finding validado (CUMPLE/REVISAR/BLOQUEANTE). El
routing_decision se deriva: hay BLOQUEANTE → 'corregir'; si no → 'aprobar'.
Degradación segura: si una lente no devuelve structured output, marca REVISAR.
"""

from dataclasses import dataclass, field

from config_loader import config
from core.schemas import Finding

LENS_PROMPTS = {
    "Riesgo": (
        "Eres el revisor de RIESGO (seguridad / PCI-DSS) del sistema dev-agent. "
        "Revisa el cambio: ¿expone datos sensibles (PAN/PIN/CVV) en logs? ¿hardcodea "
        "credenciales o tokens? ¿viola reglas PCI? ¿introduce vulnerabilidades? "
        "Marca BLOQUEANTE solo con evidencia archivo:línea de un riesgo real y alcanzable. "
        "suggestion vacío si CUMPLE."
    ),
    "Legibilidad": (
        "Eres el revisor de LEGIBILIDAD{pilar} del sistema dev-agent. "
        "¿Un dev nuevo entiende el código sin preguntar? nombres claros, una sola "
        "responsabilidad por función, sin complejidad innecesaria ni lógica muerta. "
        "El estilo solo importa si oculta un defecto concreto. Rara vez BLOQUEANTE. "
        "suggestion vacío si CUMPLE."
    ),
    "Robustez": (
        "Eres el revisor de ROBUSTEZ (manejo de errores) del sistema dev-agent. "
        "¿Todos los errores se manejan explícitamente? ¿casos borde cubiertos? "
        "¿hay panic() fuera de tests? ¿interfaces bien implementadas? "
        "BLOQUEANTE si un error no manejado puede romper en producción, con evidencia "
        "archivo:línea. suggestion vacío si CUMPLE."
    ),
    "Resiliencia": (
        "Eres el revisor de RESILIENCIA (tolerancia a fallos) del sistema dev-agent. "
        "¿Falla grácilmente ante errores externos? ¿timeouts en llamadas a servicios? "
        "¿retries donde corresponde? ¿el estado queda consistente si algo falla a mitad? "
        "BLOQUEANTE si un fallo externo deja estado inconsistente, con evidencia "
        "archivo:línea. suggestion vacío si CUMPLE."
    ),
}


@dataclass
class DimensionFinding:
    dimension: str
    status: str
    evidence: str
    suggestion: str


@dataclass
class FourRReviewResult:
    findings: list[DimensionFinding] = field(default_factory=list)

    @property
    def has_blockers(self) -> bool:
        return any(f.status == "BLOQUEANTE" for f in self.findings)

    @property
    def routing_decision(self) -> str:
        return "corregir" if self.has_blockers else "aprobar"

    @property
    def blockers(self) -> list[str]:
        return [
            f"[{f.dimension}] {f.suggestion or f.evidence}".strip()
            for f in self.findings
            if f.status == "BLOQUEANTE"
        ]

    @property
    def summary(self) -> str:
        icon = {"CUMPLE": "✅", "REVISAR": "⚠️", "BLOQUEANTE": "❌"}
        lines = ["## Revisión 4R"]
        for f in self.findings:
            lines.append(f"{icon.get(f.status, '?')} **{f.dimension}**: {f.status}")
            if f.evidence:
                lines.append(f"   Evidencia: {f.evidence}")
            if f.suggestion:
                lines.append(f"   Sugerencia: {f.suggestion}")
        return "\n".join(lines)


def _resolve_lens(prompt: str) -> str:
    """Inserta la referencia al pilar del equipo (TEAM_PILLARS_READABILITY) si existe."""
    ref = getattr(config, "TEAM_PILLARS_READABILITY", "")
    return prompt.replace("{pilar}", f" ({ref})" if ref else "")


def _lens_finding(dimension, prompt, review_input, client, model) -> DimensionFinding:
    """Corre una lente vía structured output. Degrada a REVISAR si falla."""
    from core.structured import call_structured

    finding = call_structured(
        client,
        model,
        system=prompt,
        messages=[{"role": "user", "content": f"Revisa este cambio:\n\n{review_input[:6000]}"}],
        schema_model=Finding,
        tool_name="submit_finding",
        tool_description=f"Hallazgo de la revisión de {dimension}.",
        max_tokens=512,
    )
    if finding is None:
        return DimensionFinding(
            dimension, "REVISAR", "No se pudo obtener revisión estructurada", ""
        )
    return DimensionFinding(dimension, finding.status, finding.evidence, finding.suggestion)


def run_four_r_review(review_input: str, client, model: str = None) -> FourRReviewResult | None:
    """
    Corre las 4R sobre `review_input`. Devuelve FourRReviewResult, o None si
    EVALUATOR_ENABLED está apagado. Cada lente degrada a REVISAR si falla.
    """
    if not getattr(config, "EVALUATOR_ENABLED", False):
        return None
    if model is None:
        from core.agents.base_agent import resolve_model_id

        model = resolve_model_id(
            config.MODELS.get(
                "pr_review_agent", getattr(config, "DEFAULT_MODEL", config.ANTHROPIC_MODEL)
            )
        )
    result = FourRReviewResult()
    for dimension, prompt in LENS_PROMPTS.items():
        result.findings.append(
            _lens_finding(dimension, _resolve_lens(prompt), review_input, client, model)
        )
    return result
