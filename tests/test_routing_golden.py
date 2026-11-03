"""Casos golden del router (core/evals/routing_cases.json) con planner LLM simulado.

Sin LLM, red ni DATA_DIR real: el planner se reemplaza por el plan que indica cada caso,
los agentes son falsos y la línea de decisión se captura en vez de ir a stderr.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import minka
from minka.config_loader import config
from minka.core.orchestrator import planner
from minka.core.orchestrator.orchestrator import Orchestrator
from minka.core.orchestrator.router import CURRENT_REQUEST_MARKER

CASES = json.loads(
    (Path(minka.__file__).parent / "core" / "evals" / "routing_cases.json").read_text("utf-8")
)["cases"]


class _FakeAgent:
    def __init__(self, name):
        self.name = name
        self.last_usage = {"agent": name, "model": "claude-haiku-4-5", "requests": 1}

    def run(self, task):
        return f"resultado de {self.name}"


def _task_with_history(case):
    turns = case.get("turns") or []
    if not turns:
        return case["request"]
    history = "\n".join(f"[Turno {i}] Dev: {t['request']}" for i, t in enumerate(turns, 1))
    return f"Historial de esta sesión:\n{history}\n---\n\n{CURRENT_REQUEST_MARKER}\n{case['request']}"


@pytest.fixture
def harness(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "USE_BEDROCK", False)
    monkeypatch.setattr(config, "EVALUATOR_ENABLED", False)
    monkeypatch.setattr(config, "META_AGENT_ENABLED", False)
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(config, "REPOS", {"pagos": str(tmp_path / "pagos")}, raising=False)
    state = SimpleNamespace(llm_calls=0, llm_plan=None, ran=[], lines=[])

    def fake_llm(task, context=""):
        state.llm_calls += 1
        assert CURRENT_REQUEST_MARKER not in task and "Historial" not in task
        if state.llm_plan is None:
            return None
        return [{"agent": a, "task": f"paso para {a}"} for a in state.llm_plan]

    monkeypatch.setattr(planner, "_llm_plan", fake_llm)
    orch = Orchestrator()
    monkeypatch.setattr(orch, "_get_agent", lambda n: (state.ran.append(n), _FakeAgent(n))[1])
    monkeypatch.setattr(orch, "_stderr", SimpleNamespace(print=lambda text, **kw: state.lines.append(text)))
    state.orch = orch
    return state


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_routing_golden(harness, case):
    harness.llm_plan = case.get("llm")
    harness.orch.session_turns = list(case.get("turns") or [])
    out = harness.orch.run(_task_with_history(case))
    expect = case["expect"]

    assert harness.ran == expect["agents"]
    assert harness.orch.last_router == expect["router"]
    assert (harness.llm_calls > 0) is expect["llm_called"]
    if "confidence" in expect:
        assert harness.orch.last_confidence == expect["confidence"]
    for text in expect.get("reply_contains", []):
        assert text in out
    assert len(harness.lines) == 1, harness.lines
    for text in expect.get("line_contains", []):
        assert text in harness.lines[0], harness.lines[0]
    assert harness.orch.last_run_record["router"] == expect["router"]
    assert harness.orch.last_run_record["reason"]


def test_casos_cubren_los_12_tipos_y_son_genericos():
    ids = {c["id"] for c in CASES}
    for tipo in (
        "saludo", "estado_con_turnos", "donde_se_resuelve", "flujo", "riesgos", "revisar_diff",
        "stack_trace", "implementar", "reporte", "ambiguo_sin_historial", "meta", "seguimiento_hereda",
    ):
        assert tipo in ids
    assert len(CASES) >= 20
    raw = json.dumps(CASES).lower()
    assert not any(w in raw for w in ("kushki", "credibanco", "amex", "usrv"))


def test_quiet_silencia_la_linea_de_decision(harness, monkeypatch):
    harness.orch.quiet = True
    harness.orch.run("hola")
    assert harness.lines == []


def test_read_only_rechaza_agentes_que_escriben(harness):
    harness.orch.read_only = True
    harness.llm_plan = ["development_agent"]
    out = harness.orch.run("implementa validación en internal/api/handler.go")
    assert harness.ran == [] and "solo lectura" in out
    assert harness.orch.last_run_ok is False and harness.orch.last_exit_code == 2
    harness.llm_plan = ["architecture_agent"]
    harness.orch.run("analiza internal/api/handler.go")
    assert harness.ran == ["architecture_agent"] and harness.orch.last_run_ok is True


def test_agent_forzado_es_override(harness):
    harness.llm_plan = ["development_agent"]
    harness.orch.run("arregla la agenda", forced_agent="report")
    assert harness.ran == ["report_agent"] and harness.orch.last_router == "forced"
    assert harness.llm_calls == 0 and "override --agent" in harness.lines[0]


def test_sesion_registra_turnos_y_clear_los_borra(harness):
    harness.llm_plan = ["architecture_agent"]
    harness.orch.run("analiza internal/api/handler.go")
    assert harness.orch.session_turns[0]["agents"] == ["architecture_agent"]
    harness.orch.clear_session()
    assert harness.orch.session_turns == []
