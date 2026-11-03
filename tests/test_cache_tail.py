"""Marcador móvil de caché en el bucle ReAct: system (1) + último bloque del último mensaje
user (1), TTL 5m en la cola, sin mutar SessionMemory, sin marcadores con PROMPT_CACHE_ENABLED=False.
Cliente falso que captura cada request; nunca hay red."""

import copy
from types import SimpleNamespace

import pytest

from minka.config_loader import config
from minka.core.agents import base_agent
from minka.core.agents.base_agent import BaseAgent
from minka.core.memory.session_memory import SessionMemory
from minka.core.tools.registry import ToolRegistry


class _Agent(BaseAgent):
    def get_system_prompt(self) -> str:
        return "SYSTEM " * 300


def _usage(i):
    return SimpleNamespace(input_tokens=100 + i, output_tokens=10, cache_creation_input_tokens=50,
                           cache_read_input_tokens=1000 * i, cache_creation=None)


class _Client:
    """Devuelve `n_tools` respuestas con tool_use y luego end_turn; guarda cada request."""

    def __init__(self, n_tools=2, tool="read_file", same_args=False):
        self.requests = []
        self.n_tools, self.tool, self.same_args = n_tools, tool, same_args
        self.messages = self

    def create(self, **kw):
        self.requests.append(copy.deepcopy(kw))
        i = len(self.requests)
        if i <= self.n_tools:
            args = {"path": "a.go"} if self.same_args else {"path": f"f{i}.go"}
            block = SimpleNamespace(type="tool_use", name=self.tool, input=args, id=f"t{i}")
            return SimpleNamespace(stop_reason="tool_use", content=[block], usage=_usage(i), model="m")
        text = SimpleNamespace(type="text", text="respuesta final")
        return SimpleNamespace(stop_reason="end_turn", content=[text], usage=_usage(i), model="m")


def _agent(monkeypatch, client, ttl="5m"):
    agent = _Agent.__new__(_Agent)
    agent.name = "architecture_agent"
    agent.config = config
    agent.client = client
    agent.memory = SessionMemory(agent_name=agent.name)
    agent.vector_memory = SimpleNamespace(search=lambda *a, **k: [], add=lambda *a, **k: None)
    agent.tools = [{"name": "read_file", "description": "d", "input_schema": {"type": "object"}}]
    agent.knowledge = SimpleNamespace(get_relevant_context=lambda: "KB " * 500)
    agent.iteration_count, agent.max_iterations = 0, 20
    agent.last_usage = agent._empty_usage()
    monkeypatch.setattr(config, "CACHE_TTL_BY_AGENT", {"architecture_agent": ttl}, raising=False)
    monkeypatch.setattr(config, "PROMPT_CACHE_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "QUIET", True, raising=False)
    monkeypatch.setattr(config, "TOOL_REPEAT_LIMIT", 3, raising=False)
    monkeypatch.setattr(ToolRegistry, "get", staticmethod(lambda name: lambda **kw: "contenido"))
    monkeypatch.setattr(base_agent, "resolve_repo_paths", lambda name, kwargs, repos: (kwargs, None))
    monkeypatch.setattr(base_agent, "trace", lambda *a, **k: _noop_ctx())
    monkeypatch.setattr(base_agent.progress, "track", lambda name: _noop_ctx())
    monkeypatch.setattr(base_agent.console, "print", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_log", lambda *a, **k: None)
    return agent


class _noop_ctx:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def update(self, **k):
        return None


def _markers(request):
    """(bloques del system con cache_control, [(idx_mensaje, idx_bloque, ttl)] en messages)."""
    system = request["system"]
    sys_marks = sum(1 for b in system if isinstance(b, dict) and "cache_control" in b) if isinstance(system, list) else 0
    msg_marks = []
    for mi, m in enumerate(request["messages"]):
        content = m["content"]
        if isinstance(content, list):
            for bi, b in enumerate(content):
                if isinstance(b, dict) and "cache_control" in b:
                    msg_marks.append((mi, bi, b["cache_control"].get("ttl", "5m")))
    return sys_marks, msg_marks


def test_un_marcador_en_el_ultimo_bloque_del_ultimo_user_y_el_anterior_se_quita(monkeypatch):
    client = _Client(n_tools=2)
    agent = _agent(monkeypatch, client, ttl="1h")
    assert agent.run("analiza X") == "respuesta final"
    assert len(client.requests) == 3
    for req in client.requests:
        sys_marks, msg_marks = _markers(req)
        assert sys_marks == 1 and len(msg_marks) == 1  # (1) exactamente uno en messages
        mi, bi, ttl = msg_marks[0]
        assert mi == len(req["messages"]) - 1 and req["messages"][mi]["role"] == "user"
        assert bi == len(req["messages"][mi]["content"]) - 1
        assert ttl == "5m"  # (4) cola siempre 5m aunque el system vaya a 1h
        assert req["system"][0]["cache_control"]["ttl"] == "1h"


def test_system_byte_identico_y_tarea_intacta(monkeypatch):
    client = _Client(n_tools=2)
    agent = _agent(monkeypatch, client)
    agent.run("analiza X")
    systems = [r["system"][0]["text"] for r in client.requests]
    assert len(set(systems)) == 1 and systems[0] == agent._build_system_prompt()  # (2)
    first_user = client.requests[0]["messages"][-1]["content"]
    assert first_user == [{"type": "text", "text": "analiza X", "cache_control": {"type": "ephemeral"}}]  # (3)


def test_tool_result_marcado_conserva_sus_campos(monkeypatch):
    client = _Client(n_tools=1)
    agent = _agent(monkeypatch, client)
    agent.run("analiza X")
    last = client.requests[1]["messages"][-1]["content"][-1]
    assert last["type"] == "tool_result" and last["tool_use_id"] == "t1" and last["content"] == "contenido"
    assert "is_error" not in last and last["cache_control"] == {"type": "ephemeral"}  # (6)


def test_sin_cache_no_hay_marcadores_en_messages_ni_system(monkeypatch):
    client = _Client(n_tools=1)
    agent = _agent(monkeypatch, client)
    monkeypatch.setattr(config, "PROMPT_CACHE_ENABLED", False, raising=False)
    agent.run("analiza X")
    for req in client.requests:
        assert isinstance(req["system"], str)
        assert _markers(req)[1] == []  # (5)
        assert req["messages"][0]["content"] == "analiza X"  # el string no se convierte


def test_session_memory_no_se_muta(monkeypatch):
    client = _Client(n_tools=1)
    agent = _agent(monkeypatch, client)
    agent.memory.add_exchange("tarea previa", "resultado previo")
    agent.run("analiza X")
    ctx = agent.memory.get_context()
    assert ctx[0] == {"role": "user", "content": "tarea previa"}
    assert all("cache_control" not in str(m) for m in ctx)  # (7)
    assert agent.memory._exchanges[0] == {"task": "tarea previa", "result": "resultado previo"}


def test_usage_suma_desglose_y_guarda_por_vuelta(monkeypatch):
    client = _Client(n_tools=2)
    agent = _agent(monkeypatch, client)
    agent.run("analiza X")
    assert agent.last_usage["requests"] == 3
    assert [s["cache_read_input_tokens"] for s in agent.last_usage["per_iteration"]] == [1000, 2000, 3000]
    assert agent.last_usage["cache_read_input_tokens"] == 6000  # (8)


def test_fix_loop_y_reintentos_de_tool_nunca_pasan_de_dos_marcadores(monkeypatch):
    # Reintentos: la misma tool con los mismos argumentos 5 veces (el tope bloquea desde la 4ª).
    client = _Client(n_tools=5, same_args=True)
    agent = _agent(monkeypatch, client)
    agent.run("corrige X")
    for req in client.requests:
        sys_marks, msg_marks = _markers(req)
        assert sys_marks + len(msg_marks) <= 2  # (9)
    assert agent.last_usage["repeated_tool_calls"] == 2
    # Fix-loop: tres runs seguidos del mismo agente (como hace _fix_loop) → cada request, ≤2.
    agent2 = _agent(monkeypatch, _Client(n_tools=2))
    requests = []
    for _ in range(3):
        agent2.client = _Client(n_tools=2)  # cada run del fix-loop vuelve a llamar al modelo
        agent2.run("corrige X")
        requests += agent2.client.requests
    assert len(requests) == 9
    assert all(_markers(r)[0] + len(_markers(r)[1]) <= 2 for r in requests)
    # Entre runs no quedan marcadores colgados: la primera request de cada run marca la tarea.
    for k in (0, 3, 6):
        assert _markers(requests[k])[1] == [(len(requests[k]["messages"]) - 1, 0, "5m")]


def test_marcador_no_se_pone_sobre_mensajes_assistant(monkeypatch):
    agent = _agent(monkeypatch, _Client())
    messages = [{"role": "user", "content": "x"}, {"role": "assistant", "content": [SimpleNamespace(type="text")]}]
    agent._mark_cache_tail(messages)
    assert agent._cached_block is None and "cache_control" not in str(messages[0])


@pytest.mark.parametrize("ttl", ["5m", "1h"])
def test_orden_de_ttl_system_antes_que_cola(monkeypatch, ttl):
    client = _Client(n_tools=1)
    agent = _agent(monkeypatch, client, ttl=ttl)
    agent.run("analiza X")
    req = client.requests[-1]
    assert req["system"][0]["cache_control"].get("ttl", "5m") == ttl
    assert _markers(req)[1][0][2] == "5m"
