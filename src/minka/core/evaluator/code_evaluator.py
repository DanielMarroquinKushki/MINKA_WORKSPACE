"""
code_evaluator — evalúa la calidad del código generado (Nivel 5, stub).

Combinará señales: ¿pasa go vet/golangci-lint?, ¿respeta convenciones del
knowledge base?, ¿introduce riesgos (risk_analyzer)? Devolverá un score que
alimenta el approval_gate y, en Nivel 6, al meta_agent.
"""


def evaluate_code(file_path: str, config) -> dict:
    """Evalúa un archivo generado. Devuelve {score, issues}. TODO Nivel 5."""
    if not config.EVALUATOR_ENABLED:
        raise NotImplementedError("Evaluador desactivado. Pon EVALUATOR_ENABLED=True (Nivel 5).")
    raise NotImplementedError("evaluate_code — pendiente Nivel 5.")
