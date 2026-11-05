"""
state_manager — estado compartido entre agentes durante una ejecución.

Permite que el resultado de un agente alimente al siguiente en un plan de
varios pasos (ej: architecture_agent analiza → development_agent implementa).
"""


class StateManager:
    def __init__(self):
        self._history: list[dict] = []
        # Pasos cuyo resultado se recortó en el último context_for_next (1-indexed).
        self.last_truncated_steps: list[int] = []

    def record_step(self, agent: str, task: str, result: str) -> None:
        """Registra el resultado de un step para dar contexto a los siguientes."""
        self._history.append({"agent": agent, "task": task, "result": result})

    @property
    def steps(self) -> list[dict]:
        """Historial de steps de la ejecución actual (lectura, p.ej. meta-agente)."""
        return self._history

    def context_for_next(self, max_chars: int | None = None) -> str:
        """Resultado COMPLETO de los pasos previos para el siguiente agente. `max_chars`
        (config.CONTEXT_NEXT_MAX_CHARS) recorta cada resultado y lo deja anotado."""
        self.last_truncated_steps = []
        if not self._history:
            return ""
        parts = ["Contexto de pasos previos en este plan:"]
        for i, step in enumerate(self._history, 1):
            result = step["result"] or ""
            if max_chars and len(result) > max_chars:
                self.last_truncated_steps.append(i)
                result = f"{result[:max_chars]}\n[… recortado: se muestran {max_chars} de {len(result)} caracteres]"
            parts.append(f"\n[Paso {i} — {step['agent']}]\n{result}")
        return "\n".join(parts)

    def reset(self) -> None:
        self._history.clear()

    def restore(self, history: list[dict]) -> None:
        """Restaura el historial (para reanudar un run persistido)."""
        self._history = list(history)
