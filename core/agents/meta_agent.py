"""
meta_agent — Nivel 6.

Observa la performance de los demás agentes y propone mejoras a sus system
prompts. Solo opera cuando config.META_AGENT_ENABLED = True.

Su system prompt vive en projects/<proyecto>/prompts/meta_prompt.py (Arnés
Global) — nunca modifica prompts sin aprobación del dev (gate).
"""

from core.agents.base_agent import BaseAgent
from core.prompt_loader import load_system_prompt


class MetaAgent(BaseAgent):
    def __init__(self):
        super().__init__("meta_agent")
        if not self.config.META_AGENT_ENABLED:
            raise NotImplementedError(
                "MetaAgent desactivado. Pon META_AGENT_ENABLED=True (Nivel 6)."
            )

    def get_system_prompt(self) -> str:
        return load_system_prompt("meta_agent")

    def evaluate_agents(self, session_history: list[dict]) -> str:
        """
        Evalúa la performance de todos los agentes en el historial de sesión.
        Solo se llama cuando META_AGENT_ENABLED=True.
        """
        if not session_history:
            return "No hay historial suficiente para evaluar. Ejecuta más sesiones."

        task = (
            f"Evalúa la performance de los agentes en este historial "
            f"de {len(session_history)} intercambios:\n\n"
            + "\n".join(
                f"- {h.get('agent', 'unknown')}: {h.get('task', '')[:100]}" for h in session_history
            )
        )
        return self.run(task)
