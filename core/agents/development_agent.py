"""
development_agent — escribe y refactoriza código Go en los repos.

Toda escritura pasa por el ApprovalGate: las tools de escritura/git están
envueltas en el registry como gated_* (ver core/tools/registry.py), que muestran
el diff y piden confirmación antes de tocar disco. En Nivel 3 el commit lo hace
el dev; el agente solo escribe/crea rama vía esas tools.

Los tests Go post-escritura (core/evaluator/test_runner.py) se cablearán al flujo
real en el Nivel 5 (Ola 2). Hoy NO se ejecutan tras un cambio.
"""

from core.agents.base_agent import BaseAgent
from core.prompt_loader import load_system_prompt


class DevelopmentAgent(BaseAgent):
    def __init__(self):
        super().__init__("development_agent")

    def get_system_prompt(self) -> str:
        return load_system_prompt("development_agent")
