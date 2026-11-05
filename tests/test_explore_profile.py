"""Perfil exploratorio (router llm + plan de solo architecture), tope de vueltas
ARCH_MAX_ITERATIONS con respuesta final, y registro de tools sin contenido. Sin red."""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from minka.config_loader import config
from minka.core import metrics
from minka.core.agents import base_agent
from minka.core.agents.base_agent import FINAL_TURN_BLOCK, BaseAgent
from minka.core.memory.session_memory import SessionMemory
from minka.core.orchestrator import planner, router
from minka.core.orchestrator import response_profile as rp
from minka.core.orchestrator.orchestrator import Orchestrator
from minka.core.tools.registry import ToolRegistry

ARCH = "architecture_agent"
EXPLORA = "Explora la integración del gateway: ¿qué capas intervienen? ¿Qué builders arman cada operación?"

# ---------------------------------------------------------------------------
# unidad: perfil por ruta y plan
# ---------------------------------------------------------------------------


def test_profile_exploratorio_solo_con_llm_y_plan_de_architecture():
    assert rp.profile_for("llm", ARCH, plan_agents=[ARCH]) == rp.PROFILE_EXPLORE
    assert rp.profile_for("llm", ARCH, plan_agents=[ARCH, "report_agent"]) == rp.PROFILE_FULL
    assert rp.profile_for("llm", ARCH) == rp.PROFILE_FULL  # sin plan conocido: completo
    assert rp.profile_for("info_direct", ARCH, plan_agents=[ARCH]) == rp.PROFILE_BRIEF
    assert rp.profile_for("keyword", ARCH, plan_agents=[ARCH]) == rp.PROFILE_FULL
    assert rp.profile_for("llm", "report_agent", plan_agents=["report_agent"]) == rp.PROFILE_REPORT


def test_desactivacion_en_detalle_y_force_full():
    assert rp.profile_for("llm", ARCH, force_full=True, plan_agents=[ARCH]) == rp.PROFILE_FULL
    pedido = "explícame en detalle cómo se arma el gateway"
    assert rp.profile_for("llm", ARCH, force_full=rp.wants_full(pedido), plan_agents=[ARCH]) == rp.PROFILE_FULL


def test_forced_y_bridge_siguen_en_completo():
    # _run_forced siempre pasa force_full=True (--agent y bridge).
    assert rp.profile_for("forced", ARCH, force_full=True, plan_agents=[ARCH]) == rp.PROFILE_FULL
    assert rp.profile_for("forced", ARCH, plan_agents=[ARCH]) == rp.PROFILE_FULL


def test_bloque_exploratorio_y_nota():
    block = rp.user_block(rp.PROFILE_EXPLORE)
    for texto in ("5 a 10 líneas", "Evidencia", "archivo:línea", "¿Quieres que…?"):
        assert texto in block
    assert block != rp.user_block(rp.PROFILE_BRIEF)
    assert rp.with_bounded_note("texto", rp.PROFILE_EXPLORE).endswith(rp.BOUNDED_NOTE)
    assert rp.with_bounded_note("texto", rp.PROFILE_FULL) == "texto"


def test_citas_intactas_al_recortar_el_cierre():
    reply = (
        "El gateway separa transporte y armado del request.\n\n"
        "Evidencia\n- gateway/http.go:42\n- builders/charge.go:17\n\n"
        "¿Quieres que revise también el reverso?"
    )
    out, offered, removed = rp.strip_closing_offer(reply)
    assert (offered, removed) == (True, 1)
    assert "gateway/http.go:42" in out and "builders/charge.go:17" in out
    assert "¿Quieres" not in out


# ---------------------------------------------------------------------------
# orquestador: el perfil llega al agente y el registro lleva response_lines
# ---------------------------------------------------------------------------


class _FakeAgent:
    def __init__(self, name, reply):
        self.name, self.reply, self.profiles = name, reply, []
        self.last_usage = {
            "agent": name, "model": "claude-haiku-4-5", "requests": 2, "input_tokens": 10,
            "output_tokens": 5, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
            "cache_creation_5m": 0, "cache_creation_1h": 0, "iterations_capped": 1,
        }

    def run(self, task):
        self.task = task
        self.profiles.append(getattr(self, "response_profile", None))
        return self.reply


@pytest.fixture
def orch(tmp_path, monkeypatch):
    for key, value in (
        ("USE_BEDROCK", False), ("EVALUATOR_ENABLED", False), ("META_AGENT_ENABLED", False),
        ("OUTPUT_DIR", tmp_path / "out"), ("CITATION_CHECK_ENABLED", False), ("REPOS", {}),
    ):
        monkeypatch.setattr(config, key, value, raising=False)
    o = Orchestrator()
    o.quiet = True
    agents: dict[str, _FakeAgent] = {}
    monkeypatch.setattr(o, "_get_agent", lambda name: agents.setdefault(name, _FakeAgent(name, "l1\nl2\nl3")))
    o.fake_agents = agents
    return o


def _plan(*agents):
    steps = [{"agent": a, "task": "t"} for a in agents]
    return lambda *a, **k: planner.LLMPlan(steps, None, None, 0, None)


def test_plan_llm_de_solo_architecture_usa_exploratorio(orch, monkeypatch):
    monkeypatch.setattr(planner, "_llm_plan", _plan(ARCH))
    out = orch.run(EXPLORA)
    assert orch.last_router == "llm"
    assert orch.fake_agents[ARCH].profiles == [rp.PROFILE_EXPLORE]
    assert out.endswith(rp.BOUNDED_NOTE)
    assert rp.BOUNDED_NOTE not in orch.memory.turns[-1].result
    assert orch.last_run_record["response_lines"] == 3
    assert orch.last_run_record["iterations_capped"] == 1


def test_plan_llm_con_report_queda_en_completo(orch, monkeypatch):
    monkeypatch.setattr(planner, "_llm_plan", _plan(ARCH, "report_agent"))
    # Las reglas duras quitarían el report sin palabra clave: aquí interesa el plan de 2 pasos.
    monkeypatch.setattr(router, "apply_hard_rules", lambda steps, *a, **k: (steps, []))
    out = orch.run(EXPLORA)
    assert orch.fake_agents[ARCH].profiles == [rp.PROFILE_FULL]
    assert rp.BOUNDED_NOTE not in out


def test_en_detalle_y_forzado_desactivan_el_exploratorio(orch, monkeypatch):
    monkeypatch.setattr(planner, "_llm_plan", _plan(ARCH))
    orch.run(EXPLORA + " Explícalo en detalle.")
    assert orch.fake_agents[ARCH].profiles == [rp.PROFILE_FULL]
    orch.run(EXPLORA, forced_agent="architecture")
    assert orch.last_router == "forced" and orch.fake_agents[ARCH].profiles[-1] == rp.PROFILE_FULL


def test_aggregate_suma_vueltas_con_tope():
    usage = {"per_agent": [{"agent": ARCH, "model": "claude-haiku-4-5", "requests": 1,
                            "input_tokens": 1, "output_tokens": 1, "iterations_capped": 1}], "totals": {}}
    rec = metrics.build_run_record(run_id="r", source="cli", project="p", cwd="/", router="llm",
                                   plan=[ARCH], fix_loop_iters=0, ok=True, duration_s=1.0,
                                   usage=usage, response_lines=7)
    assert (rec["iterations_capped"], rec["response_lines"]) == (1, 7)
    assert metrics.aggregate([rec, rec])["iterations_capped"] == 2


# ---------------------------------------------------------------------------
# agente: tope de vueltas con respuesta final y registro de tools sin contenido
# ---------------------------------------------------------------------------


class _Agent(BaseAgent):
    def get_system_prompt(self) -> str:
        return "SYSTEM"


def _usage(i):
    return SimpleNamespace(input_tokens=100, output_tokens=10, cache_creation_input_tokens=0,
                           cache_read_input_tokens=0, cache_creation=None)


def _has_final_block(messages) -> bool:
    content = messages[-1]["content"]
    return isinstance(content, list) and any(
        b.get("type") == "text" and b.get("text") == FINAL_TURN_BLOCK for b in content
    )


class _Client:
    """Pide tools hasta que ve el bloque de última vuelta; entonces responde."""

    def __init__(self, tool="read_file", args=None):
        self.requests, self.tool, self.args = [], tool, args or {"path": "a.go", "start_line": 1, "end_line": 40}
        self.messages = self

    def create(self, **kw):
        self.requests.append(copy.deepcopy(kw))
        i = len(self.requests)
        if _has_final_block(kw["messages"]):
            text = SimpleNamespace(type="text", text="Conclusión con cita a.go:12")
            return SimpleNamespace(stop_reason="end_turn", content=[text], usage=_usage(i), model="m")
        block = SimpleNamespace(type="tool_use", name=self.tool, input=dict(self.args, path=f"f{i}.go"), id=f"t{i}")
        return SimpleNamespace(stop_reason="tool_use", content=[block], usage=_usage(i), model="m")


class _noop_ctx:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def update(self, **k):
        return None


@pytest.fixture
def agente(monkeypatch):
    agent = _Agent.__new__(_Agent)
    agent.name = ARCH
    agent.config = config
    agent.client = _Client()
    agent.memory = SessionMemory(agent_name=ARCH)
    agent.vector_memory = SimpleNamespace(search=lambda *a, **k: [], add=lambda *a, **k: None)
    agent.tools = [{"name": "read_file", "description": "d", "input_schema": {"type": "object"}}]
    agent.knowledge = SimpleNamespace(get_relevant_context=lambda: "KB")
    agent.iteration_count, agent.max_iterations = 0, 4
    agent.last_usage = agent._empty_usage()
    monkeypatch.setattr(config, "PROMPT_CACHE_ENABLED", False, raising=False)
    monkeypatch.setattr(config, "QUIET", False, raising=False)
    monkeypatch.setattr(config, "TOOL_REPEAT_LIMIT", 3, raising=False)
    monkeypatch.setattr(ToolRegistry, "get", staticmethod(lambda name: lambda **kw: "CONTENIDO-DEVUELTO-SECRETO"))
    monkeypatch.setattr(base_agent, "resolve_repo_paths", lambda name, kwargs, repos: (kwargs, None))
    monkeypatch.setattr(base_agent, "trace", lambda *a, **k: _noop_ctx())
    monkeypatch.setattr(base_agent.progress, "track", lambda name: _noop_ctx())
    monkeypatch.setattr(base_agent.console, "print", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_log", lambda *a, **k: None)
    agent.avisos = []
    monkeypatch.setattr(base_agent.stderr_console, "print",
                        lambda text, **k: agent.avisos.append(str(text)) if "→ Tool" not in str(text) else None)
    return agent


def test_tope_pide_la_respuesta_final_y_conserva_las_citas(agente):
    result = agente.run("explora el gateway")
    assert result == "Conclusión con cita a.go:12"
    # 4 vueltas: 3 con tools (la 3ª = tope-1 lleva el bloque) y la 4ª responde.
    assert len(agente.client.requests) == 4
    assert agente.last_usage["requests"] == 4 and agente.last_usage["iterations_capped"] == 1
    assert _has_final_block(agente.client.requests[3]["messages"])
    assert not _has_final_block(agente.client.requests[2]["messages"])
    assert len(agente.avisos) == 1 and "vuelta 3 de 4" in agente.avisos[0]


def test_sin_llegar_al_tope_no_hay_bloque_ni_aviso(agente):
    agente.max_iterations = 20
    agente.client = type(agente.client)()  # mismo cliente, pero sin tope nunca verá el bloque…
    agente.client.create = _two_tools_then_answer(agente.client)
    agente.run("explora")
    assert agente.last_usage["iterations_capped"] == 0 and agente.avisos == []
    assert not any(_has_final_block(r["messages"]) for r in agente.client.requests)


def _two_tools_then_answer(client):
    def create(**kw):
        client.requests.append(copy.deepcopy(kw))
        i = len(client.requests)
        if i <= 2:
            block = SimpleNamespace(type="tool_use", name="read_file", input={"path": f"f{i}.go"}, id=f"t{i}")
            return SimpleNamespace(stop_reason="tool_use", content=[block], usage=_usage(i), model="m")
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="fin")],
                               usage=_usage(i), model="m")
    return create


def test_agotar_vueltas_devuelve_el_texto_de_la_ultima(agente):
    agente.max_iterations = 2
    agente.client.create = lambda **kw: SimpleNamespace(
        stop_reason="tool_use",
        content=[SimpleNamespace(type="text", text="parcial"),
                 SimpleNamespace(type="tool_use", name="read_file", input={"path": "x.go"}, id="t")],
        usage=_usage(0), model="m",
    )
    assert agente.run("explora") == "parcial"
    assert agente.last_usage["iterations_capped"] == 1


def test_registro_de_tools_sin_contenido(agente):
    agente.run("explora el gateway")
    per_iter = agente.last_usage["per_iteration"]
    assert [p.get("tools") for p in per_iter[:3]] == [
        [{"name": "read_file", "args": {"path": f"f{i}.go", "start_line": 1, "end_line": 40}}] for i in (1, 2, 3)
    ]
    assert "tools" not in per_iter[3]  # la vuelta final no pidió tools
    raw = json.dumps(agente.last_usage)
    assert "CONTENIDO-DEVUELTO-SECRETO" not in raw and "SYSTEM" not in raw


def test_registro_descarta_argumentos_con_contenido(agente):
    agente.last_usage["per_iteration"] = [{"input_tokens": 1}]
    blocks = [
        SimpleNamespace(type="tool_use", name="write_file", input={"path": "a.go", "content": "SECRETO" * 10}, id="w"),
        SimpleNamespace(type="tool_use", name="grep_code", input={"pattern": "token", "path": "/r", "glob": "*.go"}, id="g"),
    ]
    agente._execute_tools(SimpleNamespace(content=blocks))
    tools = agente.last_usage["per_iteration"][-1]["tools"]
    assert tools == [
        {"name": "write_file", "args": {"path": "a.go"}},
        {"name": "grep_code", "args": {"pattern": "token", "path": "/r", "glob": "*.go"}},
    ]
    assert "SECRETO" not in json.dumps(agente.last_usage)


def test_arch_max_iterations_en_config():
    assert config.ARCH_MAX_ITERATIONS == 8
