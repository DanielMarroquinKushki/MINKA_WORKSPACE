"""architecture_agent — analiza patrones, riesgos, dependencias e interfaces."""

from minka.core.agents.base_agent import BaseAgent
from minka.core.prompt_loader import load_system_prompt


class ArchitectureAgent(BaseAgent):
    def __init__(self):
        super().__init__("architecture_agent")

    def get_system_prompt(self) -> str:
        return load_system_prompt("architecture_agent")
