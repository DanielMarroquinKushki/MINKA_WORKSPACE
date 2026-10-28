"""debug_agent — troubleshooting transaccional (PIN, marcas/redes, criptografía, flujo)."""

from core.agents.base_agent import BaseAgent
from core.prompt_loader import load_system_prompt


class DebugAgent(BaseAgent):
    def __init__(self):
        super().__init__("debug_agent")

    def get_system_prompt(self) -> str:
        return load_system_prompt("debug_agent")
