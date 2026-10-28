"""pr_review_agent — revisa diffs y valida contratos, convenciones y PCI-DSS."""

from core.agents.base_agent import BaseAgent
from core.prompt_loader import load_system_prompt


class PRReviewAgent(BaseAgent):
    def __init__(self):
        super().__init__("pr_review_agent")

    def get_system_prompt(self) -> str:
        return load_system_prompt("pr_review_agent")
