"""Tope de llamadas idénticas a una tool por corrida (TOOL_REPEAT_LIMIT): la 4ª no se ejecuta,
devuelve error al modelo, avisa por stderr y se cuenta en el usage."""

from types import SimpleNamespace

import pytest

from minka.config_loader import config
from minka.core.agents import base_agent
from minka.core.agents.base_agent import BaseAgent
from minka.core.tools.registry import ToolRegistry


class _Agent(BaseAgent):
    def get_system_prompt(self) -> str:
        return ""


def _agent():
    agent = _Agent.__new__(_Agent)  # sin __init__: ni cliente LLM, ni knowledge, ni tools
    agent.name = "demo_agent"
    agent.config = config
    agent.last_usage = agent._empty_usage()
    return agent


def _response(n, path="a.go"):
    blocks = [
        SimpleNamespace(type="tool_use", name="read_file", input={"path": path}, id=f"t{i}")
        for i in range(n)
    ]
    return SimpleNamespace(content=blocks)


@pytest.fixture
def ejecuciones(monkeypatch):
    calls = []
    monkeypatch.setattr(ToolRegistry, "get", staticmethod(lambda name: lambda **kw: calls.append(kw) or "ok"))
    monkeypatch.setattr(base_agent, "resolve_repo_paths", lambda name, kwargs, repos: (kwargs, None))
    monkeypatch.setattr(config, "TOOL_REPEAT_LIMIT", 3, raising=False)
    monkeypatch.setattr(config, "QUIET", False, raising=False)
    monkeypatch.setattr(base_agent.console, "print", lambda *a, **k: None)
    return calls


def test_cuarta_llamada_identica_se_bloquea(ejecuciones, monkeypatch):
    avisos = []
    monkeypatch.setattr(base_agent.stderr_console, "print", lambda text, **k: avisos.append(text))
    agent = _agent()
    results = agent._execute_tools(_response(5))
    assert len(ejecuciones) == 3  # solo las tres primeras se ejecutaron
    assert [r.get("is_error", False) for r in results] == [False, False, False, True, True]
    assert "Llamada repetida: read_file" in results[3]["content"]
    assert agent.last_usage["repeated_tool_calls"] == 2
    assert len(avisos) == 2 and "demo_agent" in avisos[0]


def test_argumentos_distintos_no_cuentan_como_repeticion(ejecuciones):
    agent = _agent()
    for path in ("a.go", "b.go", "c.go", "d.go"):
        agent._execute_tools(_response(1, path))
    assert len(ejecuciones) == 4 and agent.last_usage["repeated_tool_calls"] == 0


def test_quiet_no_avisa_pero_si_bloquea(ejecuciones, monkeypatch):
    monkeypatch.setattr(config, "QUIET", True, raising=False)
    avisos = []
    monkeypatch.setattr(base_agent.stderr_console, "print", lambda text, **k: avisos.append(text))
    agent = _agent()
    agent._execute_tools(_response(4))
    assert len(ejecuciones) == 3 and avisos == [] and agent.last_usage["repeated_tool_calls"] == 1
