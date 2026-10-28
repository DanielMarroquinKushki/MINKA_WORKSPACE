"""
agent_evaluator — evalúa la performance de cada agente (Nivel 6, stub).

Analiza output/logs/agent_runs.jsonl y métricas de resultados (tests pasados,
riesgos detectados vs perdidos) para puntuar a cada agente. Insumo del meta_agent.
"""


class AgentEvaluator:
    def __init__(self, config):
        if not config.META_AGENT_ENABLED:
            raise NotImplementedError(
                "Meta-agente desactivado. Pon META_AGENT_ENABLED=True (Nivel 6)."
            )
        raise NotImplementedError("AgentEvaluator — pendiente Nivel 6.")
