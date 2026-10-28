"""report_agent — genera reportes técnicos en Markdown."""

from minka.core.agents.base_agent import BaseAgent
from minka.core.prompt_loader import load_system_prompt


class ReportAgent(BaseAgent):
    def __init__(self):
        super().__init__("report_agent")

    def get_system_prompt(self) -> str:
        return load_system_prompt("report_agent")
