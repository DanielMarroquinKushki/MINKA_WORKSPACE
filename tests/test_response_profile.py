"""Concisión por tipo de pedido (core/orchestrator/response_profile.py) y gancho
post-respuesta en `Orchestrator._record_turn`. Sin LLM: agentes y cliente falsos."""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from minka import main as minka_main
from minka.config_loader import config
from minka.core import metrics
from minka.core.agents import base_agent
from minka.core.agents.base_agent import BaseAgent
from minka.core.memory.session_memory import SessionMemory
from minka.core.orchestrator import planner
from minka.core.orchestrator import response_profile as rp
from minka.core.orchestrator.orchestrator import Orchestrator
from minka.core.tools.registry import ToolRegistry

# ---------------------------------------------------------------------------
# unidad: perfiles, detección de "en detalle" y recorte del cierre
# ---------------------------------------------------------------------------


def test_profile_for_por_ruta_y_agente():
    assert rp.profile_for("info_direct", "architecture_agent") == rp.PROFILE_BRIEF
    assert rp.profile_for("llm", "architecture_agent") == rp.PROFILE_FULL
    assert rp.profile_for("forced", "architecture_agent") == rp.PROFILE_FULL
    assert rp.profile_for("resume", "development_agent") == rp.PROFILE_FULL
    assert rp.profile_for("info_direct", "report_agent") == rp.PROFILE_REPORT
    assert rp.profile_for("info_direct", "architecture_agent", force_full=True) == rp.PROFILE_FULL


def test_wants_full_detecta_en_detalle_y_completo():
    assert rp.wants_full("explícame en detalle el flujo de compra")
    assert rp.wants_full("dame el análisis completo")
    assert not rp.wants_full("¿dónde se resuelve el token?")
    assert not rp.wants_full("completar el formulario")  # palabra distinta, no fuerza


def test_user_block_solo_en_breve():
    assert "citas archivo:línea" in rp.user_block(rp.PROFILE_BRIEF)
    assert rp.user_block(rp.PROFILE_FULL) == ""
    assert "resultado de los pasos previos" in rp.user_block(rp.PROFILE_REPORT)
    assert rp.user_block("") == ""


@pytest.mark.parametrize(
    "closing",
    [
        "¿Quieres que revise también el repo hsm?",
        "¿Te gustaría que lo detalle?",
        "¿Deseas ver el diff?",
        "¿Necesitas algo más?",
        "¿Te sirve este resumen?",
        "**¿Quieres que siga con el reverso?**",
        "- ¿Quieres que lo implemente?",
    ],
)
def test_strip_closing_offer_quita_la_oferta_final(closing):
    body = "El token se resuelve en service/token.go:42.\n\nConclusión: viene del authorizer."
    text, offered, removed = rp.strip_closing_offer(f"{body}\n\n{closing}\n")
    assert (text, offered, removed) == (body, True, 1)


def test_strip_closing_offer_respeta_pregunta_aclaratoria():
    text = "Hay dos candidatos.\n\n¿Cuál de los dos repos quieres que mire, pos o hsm?"
    assert rp.strip_closing_offer(text) == (text, False, 0)


def test_strip_closing_offer_nunca_borra_una_linea_con_cita():
    text = "Resumen.\n\n¿Quieres que revise service/token.go:42?"
    assert rp.strip_closing_offer(text) == (text, False, 0)


def test_strip_closing_offer_solo_la_ultima_linea():
    text = "¿Quieres que empiece?\n\nEl flujo va por gateway/sender.go:10.\n\nFin."
    assert rp.strip_closing_offer(text) == (text, False, 0)
    assert rp.strip_closing_offer("") == ("", False, 0)
    assert rp.strip_closing_offer("Sin pregunta al final.") == ("Sin pregunta al final.", False, 0)


def test_with_bounded_note_solo_en_breve():
    assert rp.with_bounded_note("x", rp.PROFILE_BRIEF).endswith(rp.BOUNDED_NOTE)
    assert rp.with_bounded_note("x", rp.PROFILE_FULL) == "x"
    assert rp.with_bounded_note("", rp.PROFILE_BRIEF) == ""


# ---------------------------------------------------------------------------
# orquestador: perfil por ruta, nota mostrada, memoria y registro
# ---------------------------------------------------------------------------


class _FakeAgent:
    def __init__(self, name, reply):
        self.name = name
        self.reply = reply
        self.profiles: list[str | None] = []
        self.last_usage = {
            "agent": name, "model": "claude-haiku-4-5", "requests": 1, "input_tokens": 10,
            "output_tokens": 5, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
            "cache_creation_5m": 0, "cache_creation_1h": 0,
        }

    def run(self, task):
        self.task = task
        self.profiles.append(getattr(self, "response_profile", None))
        return self.reply


REPLY = "El token se resuelve en service/token.go:42.\n\n¿Quieres que revise también el hsm?"


def _git_repo(path):
    import subprocess

    path.mkdir()
    (path / "main.go").write_text("package main\n")
    for args in (("init", "-q", "-b", "main"), ("config", "user.email", "t@example.com"),
                 ("config", "user.name", "t"), ("add", "."), ("commit", "-q", "-m", "init")):
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


@pytest.fixture
def orch(tmp_path, monkeypatch):
    repos = {}
    for name in ("pos", "hsm", "core"):
        _git_repo(tmp_path / name)
        repos[name] = str(tmp_path / name)
    monkeypatch.setattr(config, "USE_BEDROCK", False)
    monkeypatch.setattr(config, "EVALUATOR_ENABLED", False)
    monkeypatch.setattr(config, "META_AGENT_ENABLED", False)
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path / "out")
    monkeypatch.setattr(config, "REPOS", repos, raising=False)
    monkeypatch.setattr(planner, "_llm_plan", lambda *a, **k: pytest.fail("el planner no debía ejecutarse"))
    o = Orchestrator()
    o.quiet = True
    agents: dict[str, _FakeAgent] = {}

    def get_agent(name):
        agents.setdefault(name, _FakeAgent(name, REPLY))
        return agents[name]

    monkeypatch.setattr(o, "_get_agent", get_agent)
    o.fake_agents = agents
    return o


def test_info_direct_usa_breve_quita_cierre_y_anota_solo_en_pantalla(orch):
    out = orch.run("¿dónde se resuelve el token?")
    assert orch.last_router == "info_direct"
    assert orch.fake_agents["architecture_agent"].profiles == [rp.PROFILE_BRIEF]
    assert "¿Quieres" not in out and out.endswith(rp.BOUNDED_NOTE)
    assert "service/token.go:42" in out
    stored = orch.memory.turns[-1].result
    assert "¿Quieres" not in stored and rp.BOUNDED_NOTE not in stored
    assert (orch.last_ends_with_offer, orch.last_closings_removed) == (True, 1)
    assert orch.last_run_record["ends_with_offer"] is True
    assert orch.last_run_record["closings_removed"] == 1


def test_en_detalle_fuerza_completo(orch):
    out = orch.run("¿dónde se resuelve el token? explícalo en detalle")
    assert orch.fake_agents["architecture_agent"].profiles == [rp.PROFILE_FULL]
    assert rp.BOUNDED_NOTE not in out
    assert "¿Quieres" not in out  # el recorte del cierre aplica a todos los perfiles


def test_force_full_apaga_el_perfil_breve(orch):
    orch.force_full = True
    out = orch.run("¿dónde se resuelve el token?")
    assert orch.fake_agents["architecture_agent"].profiles == [rp.PROFILE_FULL]
    assert rp.BOUNDED_NOTE not in out


def test_agente_forzado_con_consulta_simple_es_breve(orch):
    # --agent con una pregunta de información simple: perfil breve (misma regla que info_direct).
    out = orch.run("¿dónde se resuelve el token?", forced_agent="architecture")
    assert orch.last_router == "forced" and orch.last_reason == "override --agent · consulta breve"
    assert orch.fake_agents["architecture_agent"].profiles == [rp.PROFILE_BRIEF]
    assert out.endswith(rp.BOUNDED_NOTE) and "¿Quieres" not in out
    assert orch.last_run_record["closings_removed"] == 1


def test_agente_forzado_con_pedido_largo_queda_en_completo(orch):
    out = orch.run("analiza el flujo de pagos y luego documenta los riesgos", forced_agent="architecture")
    assert orch.last_reason == "override --agent"
    assert orch.fake_agents["architecture_agent"].profiles == [rp.PROFILE_FULL]
    assert rp.BOUNDED_NOTE not in out


def test_report_forzado_conserva_su_formato(orch):
    orch.run("resume la sesión", forced_agent="report")
    assert orch.fake_agents["report_agent"].profiles == [rp.PROFILE_REPORT]


def test_pregunta_aclaratoria_llega_intacta(orch, monkeypatch):
    reply = "Hay dos candidatos en pos y hsm.\n\n¿Cuál de los dos repos quieres que mire?"
    monkeypatch.setattr(orch, "_get_agent", lambda name: _FakeAgent(name, reply))
    out = orch.run("¿dónde se resuelve el token?")
    assert "¿Cuál de los dos repos" in out
    assert (orch.last_ends_with_offer, orch.last_closings_removed) == (False, 0)


def test_respuestas_de_costo_cero_no_tocan_contadores(orch):
    orch.run("hola")
    assert orch.last_router == "small_talk"
    assert (orch.last_ends_with_offer, orch.last_closings_removed) == (False, 0)


def test_plantilla_de_forma_solo_en_consultas_tecnicas(orch):
    """La plantilla viaja en el bloque breve: saludos, aclaraciones y no_changes no corren
    agente, así que nunca la reciben; la consulta técnica sí."""
    assert "Forma, solo las partes que apliquen" in rp.BRIEF_BLOCK
    for req, route in (("hola", "small_talk"), ("¿y en el otro repo?", "ambiguous")):
        orch.run(req)
        assert orch.last_router == route and orch.fake_agents == {}, req
    orch.run("revisa este diff")
    assert orch.last_router == "no_changes" and orch.fake_agents == {}
    orch.run("¿dónde se resuelve el token?")
    assert orch.fake_agents["architecture_agent"].profiles == [rp.PROFILE_BRIEF]


# ---------------------------------------------------------------------------
# agente: el bloque va en el mensaje user; el system queda byte-idéntico
# ---------------------------------------------------------------------------


class _Agent(BaseAgent):
    def get_system_prompt(self) -> str:
        return "SYSTEM " * 50


class _Client:
    def __init__(self):
        self.requests = []
        self.messages = self

    def create(self, **kw):
        self.requests.append(copy.deepcopy(kw))
        usage = SimpleNamespace(input_tokens=10, output_tokens=5, cache_creation_input_tokens=0,
                                cache_read_input_tokens=0, cache_creation=None)
        text = SimpleNamespace(type="text", text="respuesta final")
        return SimpleNamespace(stop_reason="end_turn", content=[text], usage=usage, model="m")


class _noop_ctx:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def update(self, **k):
        pass


def _agent(monkeypatch):
    client = _Client()
    agent = _Agent.__new__(_Agent)
    agent.name = "architecture_agent"
    agent.config = config
    agent.client = client
    agent.memory = SessionMemory(agent_name=agent.name)
    agent.vector_memory = SimpleNamespace(search=lambda *a, **k: [], add=lambda *a, **k: None)
    agent.tools = [{"name": "read_file", "description": "d", "input_schema": {"type": "object"}}]
    agent.knowledge = SimpleNamespace(get_relevant_context=lambda: "KB " * 50)
    agent.conversation = []
    agent.response_profile = None
    agent.iteration_count, agent.max_iterations = 0, 5
    agent.last_usage = agent._empty_usage()
    monkeypatch.setattr(config, "PROMPT_CACHE_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "QUIET", True, raising=False)
    monkeypatch.setattr(ToolRegistry, "get", staticmethod(lambda name: lambda **kw: "contenido"))
    monkeypatch.setattr(base_agent, "trace", lambda *a, **k: _noop_ctx())
    monkeypatch.setattr(base_agent.progress, "track", lambda name: _noop_ctx())
    monkeypatch.setattr(base_agent.console, "print", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_log", lambda *a, **k: None)
    return agent, client


def _user_text(request):
    content = request["messages"][-1]["content"]
    return content if isinstance(content, str) else content[-1]["text"]


def test_bloque_breve_va_en_user_y_el_system_es_identico(monkeypatch):
    agent, client = _agent(monkeypatch)
    agent.run("¿dónde se resuelve el token?")
    agent.response_profile = rp.PROFILE_BRIEF
    agent.run("¿dónde se resuelve el token?")
    full, brief = client.requests
    assert full["system"] == brief["system"]
    assert full["tools"] == brief["tools"]
    assert _user_text(full) == "¿dónde se resuelve el token?"
    assert _user_text(brief).startswith("¿dónde se resuelve el token?")
    assert _user_text(brief).endswith(rp.BRIEF_BLOCK)
    assert agent.response_profile is None  # un solo uso por run


def test_perfil_completo_no_agrega_nada(monkeypatch):
    agent, client = _agent(monkeypatch)
    agent.response_profile = rp.PROFILE_FULL
    agent.run("tarea")
    assert _user_text(client.requests[0]) == "tarea"


# ---------------------------------------------------------------------------
# métricas y CLI
# ---------------------------------------------------------------------------


def _record(**kw):
    base = dict(run_id="r", source="cli", project="demo", cwd="/", router="info_direct", plan=["a"],
                fix_loop_iters=0, ok=True, duration_s=1.0, usage={"per_agent": [], "totals": {}})
    base.update(kw)
    return metrics.build_run_record(**base)


def test_build_run_record_y_aggregate_llevan_los_contadores():
    r1 = _record(ends_with_offer=True, closings_removed=1)
    r2 = _record()
    assert (r1["ends_with_offer"], r1["closings_removed"]) == (True, 1)
    assert (r2["ends_with_offer"], r2["closings_removed"]) == (False, 0)
    agg = metrics.aggregate([r1, r2, r1])
    assert (agg["ends_with_offer"], agg["closings_removed"]) == (2, 2)


class _FakeOrch:
    def __init__(self):
        self.memory = []
        self.last_run_record = {}
        self.last_run_ok = True

    def run(self, task, **kwargs):
        return "ok"


@pytest.mark.parametrize("flag", ["--completo", "--debug"])
def test_cli_completo_y_debug_fuerzan_el_modo_completo(monkeypatch, tmp_path, flag):
    fake = _FakeOrch()
    monkeypatch.setattr(minka_main, "_require_llm_backend", lambda: None)
    monkeypatch.setattr(minka_main, "_make_orchestrator", lambda: fake)
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
    result = CliRunner().invoke(minka_main.app, ["analiza X", flag, "--quiet"])
    assert result.exit_code == 0, result.output
    assert fake.force_full is True


def test_cli_sin_flags_no_fuerza(monkeypatch, tmp_path):
    fake = _FakeOrch()
    monkeypatch.setattr(minka_main, "_require_llm_backend", lambda: None)
    monkeypatch.setattr(minka_main, "_make_orchestrator", lambda: fake)
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
    assert CliRunner().invoke(minka_main.app, ["analiza X", "--quiet"]).exit_code == 0
    assert fake.force_full is False
