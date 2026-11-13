"""Lote 5, sin red: (1) --agent con una consulta de información simple usa el perfil breve;
(2) "¿y en el repo hsm?" tras una consulta útil de architecture repite el análisis sobre ese
repo sin planner (router inherit_repo). Ninguna de las dos piezas toca prompts, tools ni
MODELS: el prompt_hash no se mueve."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from minka.config_loader import config
from minka.core.orchestrator import planner, router
from minka.core.orchestrator import response_profile as rp
from minka.core.orchestrator.orchestrator import Orchestrator
from minka.main import _write_json_out

ARCH = "architecture_agent"
REPOS = {"pos": "/tmp/pos", "hsm": "/tmp/hsm", "core": "/tmp/core", "payment-crypto": "/tmp/pc"}
PREGUNTA = "¿dónde se firma el request?"
CON_CITA = "El request se firma en internal/sign/jws.go:58 dentro de signRequest()."


class _FakeAgent:
    def __init__(self, name, reply=CON_CITA):
        self.name, self.reply, self.profiles, self.tasks = name, reply, [], []
        self.last_usage = {
            "agent": name, "model": "claude-haiku-4-5", "requests": 1, "input_tokens": 10,
            "output_tokens": 5, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
            "cache_creation_5m": 0, "cache_creation_1h": 0,
        }

    def run(self, task):
        self.tasks.append(task)
        self.profiles.append(getattr(self, "response_profile", None))
        return self.reply


@pytest.fixture
def orch(tmp_path, monkeypatch):
    for key, value in (
        ("USE_BEDROCK", False), ("EVALUATOR_ENABLED", False), ("META_AGENT_ENABLED", False),
        ("OUTPUT_DIR", tmp_path / "out"), ("CITATION_CHECK_ENABLED", False), ("REPOS", dict(REPOS)),
    ):
        monkeypatch.setattr(config, key, value, raising=False)
    monkeypatch.setattr(planner, "_llm_plan", lambda *a, **k: pytest.fail("el planner no debía ejecutarse"))
    o = Orchestrator()
    state = SimpleNamespace(orch=o, agents={}, lines=[])
    monkeypatch.setattr(o, "_get_agent", lambda name: state.agents.setdefault(name, _FakeAgent(name)))
    monkeypatch.setattr(o, "_stderr", SimpleNamespace(print=lambda text, **kw: state.lines.append(text)))
    return state


# ---------------------------------------------------------------------------
# (1) --agent: consulta breve
# ---------------------------------------------------------------------------


def test_agente_forzado_con_pregunta_usa_perfil_breve(orch):
    out = orch.orch.run(PREGUNTA, forced_agent="architecture")
    o = orch.orch
    assert o.last_router == "forced" and o.last_confidence == "alta"
    assert o.last_reason == "override --agent · consulta breve"
    assert orch.agents[ARCH].profiles == [rp.PROFILE_BRIEF]
    assert out.endswith(rp.BOUNDED_NOTE)
    assert rp.BOUNDED_NOTE not in o.memory.turns[-1].result
    assert "→ architecture (override --agent · consulta breve)" in orch.lines[-1]
    assert o.last_run_record["router"] == "forced" and o.last_run_ok and o.last_exit_code is None


@pytest.mark.parametrize(
    "pedido",
    [
        "genera un reporte de cómo se firma el request",
        "analiza el flujo de firma y luego implementa el retry",
        "¿por qué falla la firma del request?",
    ],
)
def test_agente_forzado_sin_consulta_simple_sigue_completo(orch, pedido):
    out = orch.orch.run(pedido, forced_agent="architecture")
    assert orch.orch.last_reason == "override --agent"
    assert orch.agents[ARCH].profiles == [rp.PROFILE_FULL]
    assert rp.BOUNDED_NOTE not in out


def test_completo_debug_y_en_detalle_apagan_el_breve(orch):
    orch.orch.force_full = True  # main.py: --completo o --debug
    orch.orch.run(PREGUNTA, forced_agent="architecture")
    assert orch.agents[ARCH].profiles == [rp.PROFILE_FULL]
    assert orch.orch.last_reason == "override --agent"
    orch.orch.force_full = False
    orch.orch.run(f"{PREGUNTA} en detalle", forced_agent="architecture")
    assert orch.agents[ARCH].profiles[-1] == rp.PROFILE_FULL


def test_report_forzado_con_pregunta_mantiene_perfil_reporte(orch):
    orch.orch.run(PREGUNTA, forced_agent="report")
    assert orch.agents["report_agent"].profiles == [rp.PROFILE_REPORT]
    assert orch.orch.last_reason == "override --agent · consulta breve"


def test_read_only_y_json_out_no_cambian_con_el_breve(orch, tmp_path):
    o = orch.orch
    o.read_only = True
    result = o.run(PREGUNTA, forced_agent="architecture")
    assert o.last_run_ok and o.last_exit_code is None
    path = tmp_path / "out.json"
    assert _write_json_out(path, PREGUNTA, "architecture", result, o.last_run_ok, o.last_usage, o.last_run_record)
    payload = json.loads(path.read_text())
    assert set(payload) == {
        "ok", "agent", "task", "result", "usage", "run_id", "router", "reason", "confidence",
        "duration_s", "cost_usd", "baseline_usd", "pricing_version",
    }
    assert payload["router"] == "forced" and payload["reason"] == "override --agent · consulta breve"


# ---------------------------------------------------------------------------
# (2) seguimiento entre repos
# ---------------------------------------------------------------------------


def test_sole_repo_alias_y_is_repo_followup():
    assert router.sole_repo_alias("¿y en el repo hsm?", REPOS) == "hsm"
    assert router.sole_repo_alias("¿y en payment-crypto?", REPOS) == "payment-crypto"
    assert router.sole_repo_alias("¿y en hsm y core?", REPOS) is None  # dos alias
    assert router.sole_repo_alias("¿y en hsm/internal/sign.go?", REPOS) is None  # otra referencia
    assert router.sole_repo_alias("¿y en el otro repo?", REPOS) is None
    assert router.sole_repo_alias("¿y en el repo hsm?", {}) is None
    assert router.is_repo_followup("¿y en el repo hsm?", REPOS) == "hsm"
    assert router.is_repo_followup("¿y el core?", REPOS) == "core"
    for pedido in (
        "¿y en el repo hsm cómo se firma el request?",  # pregunta propia
        "arregla eso en el repo hsm",  # verbo de escritura
        "¿y por qué falla en hsm?",  # fallo
        "revisa el diff de hsm",  # diff
        "genera un reporte de hsm",  # reporte
        "ahora quiero que me cuentes todo lo que sabes del módulo que vive en hsm",  # largo, sin arranque
    ):
        assert router.is_repo_followup(pedido, REPOS) is None, pedido


def test_inherit_repo_repite_el_pedido_sobre_el_alias(orch):
    o = orch.orch
    o.run(PREGUNTA)
    assert o.last_router == "info_direct"
    out = o.run("¿y en el repo hsm?")
    assert o.last_router == "inherit_repo" and o.last_confidence == "media"
    assert o.last_reason == "mismo pedido, otro repo"
    assert "→ architecture (mismo pedido, otro repo) · repo hsm" in orch.lines[-1]
    assert orch.agents[ARCH].tasks[-1].endswith("Repite el análisis anterior sobre el repo hsm")
    assert o.last_run_record["router"] == "inherit_repo" and o.last_run_record["tokens"]["requests"] == 1
    assert out  # respuesta del agente, sin pedir precisar


def test_inherit_repo_exige_citas_en_el_turno_previo(orch, monkeypatch):
    o = orch.orch
    o.session_turns = [{"request": PREGUNTA, "agents": [ARCH], "useful": True, "result": "Sin citas."}]
    monkeypatch.setattr(planner, "_llm_plan", lambda *a, **k: planner.LLMPlan([{"agent": ARCH, "task": "t"}]))
    o.run("¿y en el repo hsm?")
    assert o.last_router == "llm"  # sin citas no hay análisis que repetir: planner


def test_inherit_repo_no_hereda_de_pr_review_ni_development(orch, monkeypatch):
    o = orch.orch
    monkeypatch.setattr(planner, "_llm_plan", lambda *a, **k: planner.LLMPlan([{"agent": ARCH, "task": "t"}]))
    for previo in ("pr_review_agent", "development_agent"):
        o.session_turns = [{"request": "revisa el diff", "agents": [previo], "useful": True, "result": CON_CITA}]
        o.run("¿y en el repo hsm?")
        assert o.last_router == "llm", previo


def test_inherit_repo_sin_historial_pide_precisar(orch):
    o = orch.orch
    out = o.run("¿y en el repo hsm?")
    assert o.last_router == "ambiguous" and o.last_confidence == "baja"
    assert "ambiguo" in out and orch.agents == {}
    assert o.last_run_record["cost_usd"] == 0.0


def test_inherit_repo_aparece_en_reglas_evaluadas():
    assert "inherit_repo" in router.LOCAL_RULES
    assert router.LOCAL_RULES.index("inherit_repo") < router.LOCAL_RULES.index("ambiguous")
