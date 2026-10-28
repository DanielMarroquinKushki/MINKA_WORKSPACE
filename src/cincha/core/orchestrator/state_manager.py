"""
state_manager — estado compartido entre agentes durante una ejecución.

Permite que el resultado de un agente alimente al siguiente en un plan de
varios pasos (ej: architecture_agent analiza → development_agent implementa).
"""


class StateManager:
    def __init__(self):
        self._history: list[dict] = []

    def record_step(self, agent: str, task: str, result: str) -> None:
        """Registra el resultado de un step para dar contexto a los siguientes."""
        self._history.append({"agent": agent, "task": task, "result": result})

    @property
    def steps(self) -> list[dict]:
        """Historial de steps de la ejecución actual (lectura, p.ej. meta-agente)."""
        return self._history

    def context_for_next(self) -> str:
        """Resumen de los pasos previos, para inyectar al siguiente agente."""
        if not self._history:
            return ""
        parts = ["Contexto de pasos previos en este plan:"]
        for i, step in enumerate(self._history, 1):
            parts.append(f"\n[Paso {i} — {step['agent']}]\n{step['result'][:1500]}")
        return "\n".join(parts)

    def reset(self) -> None:
        self._history.clear()

    def restore(self, history: list[dict]) -> None:
        """Restaura el historial (para reanudar un run persistido)."""
        self._history = list(history)
