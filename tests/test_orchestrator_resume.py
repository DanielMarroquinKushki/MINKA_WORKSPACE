"""Tests offline del checkpoint/resume del orquestador (sin LLM ni SSO).

Simulan un run interrumpido y verifican que al reanudar se saltan los pasos ya
completados, y que el estado se limpia al terminar.
"""

from cincha.config_loader import config
from cincha.core.orchestrator.orchestrator import Orchestrator


class _FakeAgent:
    def __init__(self, name):
        self.name = name

    def run(self, task):
        return f"resultado de {self.name}"


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "USE_BEDROCK", False)  # sin gate de sesión
    monkeypatch.setattr(config, "EVALUATOR_ENABLED", False)  # sin fix_loop
    monkeypatch.setattr(config, "META_AGENT_ENABLED", False)
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)  # estado aislado


def test_resume_skips_completed_steps(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    orch = Orchestrator()
    calls = []
    monkeypatch.setattr(orch, "_get_agent", lambda name: (calls.append(name), _FakeAgent(name))[1])

    plan = [
        {"agent": "architecture_agent", "task": "t1"},
        {"agent": "report_agent", "task": "t2"},
    ]
    # Simula un run interrumpido: el paso 0 (architecture) ya se completó.
    orch._save_state("mi tarea", plan, ["### [architecture_agent]\nya hecho"], 1, False)

    out = orch.run("mi tarea", use_llm_planner=False)
    assert calls == ["report_agent"]  # solo el paso no-completado
    assert "ya hecho" in out and "report_agent" in out
    assert not orch._state_path().is_file()  # se limpia al terminar


def test_fresh_run_clears_state(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    orch = Orchestrator()
    calls = []
    monkeypatch.setattr(orch, "_get_agent", lambda name: (calls.append(name), _FakeAgent(name))[1])

    # "analiza riesgos" → routing por keyword → architecture_agent
    orch.run("analiza riesgos", use_llm_planner=False)
    assert calls == ["architecture_agent"]
    assert not orch._state_path().is_file()
