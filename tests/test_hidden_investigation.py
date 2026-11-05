"""Planes de varios pasos: solo se muestra el último paso, la investigación queda en memoria de
la sesión (/detalle, /detalle N), línea resumen siempre visible. Perfil decisión, verificador
con "(doc)" y sugerencia de basename en las citas. Sin red ni modelo."""

from __future__ import annotations

import json
import subprocess

import pytest
from typer.testing import CliRunner

from minka import main as minka_main
from minka.config_loader import config
from minka.core.orchestrator import citation_check, planner, router
from minka.core.orchestrator import claim_check as cc
from minka.core.orchestrator import orchestrator as orch_mod
from minka.core.orchestrator import response_profile as rp
from minka.core.orchestrator.orchestrator import Orchestrator
from minka.core.repl_input import ReplReader
from minka.core.tools.analysis.citations import RepoIndex

ARCH, REPORT = "architecture_agent", "report_agent"
PLAN_2 = [{"agent": ARCH, "task": "t1"}, {"agent": REPORT, "task": "t2"}]
ARCH_REPLY = "\n".join(f"hallazgo {i} en pos/token.go:2" for i in range(130))  # 130 líneas
REPORT_REPLY = "Conclusión: la WK rota en `ResolveToken()`.\n\nHallazgos clave\n- pos/token.go:2"


def _git_repo(path, files):
    path.mkdir()
    for name, content in files.items():
        (path / name).write_text(content)
    for args in (("init", "-q", "-b", "main"), ("config", "user.email", "t@example.com"),
                 ("config", "user.name", "t"), ("add", "."), ("commit", "-q", "-m", "init")):
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


class _FakeAgent:
    def __init__(self, name):
        self.name, self.profiles, self.tasks = name, [], []
        self.last_usage = {
            "agent": name, "model": "claude-haiku-4-5", "requests": 4 if name == ARCH else 1,
            "input_tokens": 1, "output_tokens": 1, "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0, "cache_creation_5m": 0, "cache_creation_1h": 0,
        }

    def run(self, task):
        self.tasks.append(task)
        self.profiles.append(getattr(self, "response_profile", None))
        return ARCH_REPLY if self.name == ARCH else REPORT_REPLY


@pytest.fixture
def orch(tmp_path, monkeypatch):
    _git_repo(tmp_path / "pos", {"token.go": "package svc\nfunc ResolveToken() {}\n"})
    for key, value in (
        ("USE_BEDROCK", False), ("EVALUATOR_ENABLED", False), ("META_AGENT_ENABLED", False),
        ("OUTPUT_DIR", tmp_path / "out"), ("REPOS", {"pos": str(tmp_path / "pos")}),
        ("VERIFY_CLAIMS_ENABLED", True), ("PROJECTS_DIR", tmp_path / "projects"),
    ):
        monkeypatch.setattr(config, key, value, raising=False)
    monkeypatch.setattr(planner, "_llm_plan", lambda *a, **k: planner.LLMPlan([dict(s) for s in PLAN_2], None, None, 0, None))
    monkeypatch.setattr(router, "apply_hard_rules", lambda steps, *a, **k: (steps, []))
    o = Orchestrator()
    o.quiet = True
    o.hide_investigation = True
    agents = {}
    monkeypatch.setattr(o, "_get_agent", lambda name: agents.setdefault(name, _FakeAgent(name)))
    o.fake_agents = agents
    printed = []
    monkeypatch.setattr(orch_mod.console, "print", lambda *a, **k: printed.append(str(a[0]) if a else ""))
    o.printed = printed
    return o


def test_plan_de_dos_pasos_muestra_solo_el_ultimo_y_guarda_la_investigacion(orch):
    out = orch.run("genera un reporte del flujo de compra")
    assert out.startswith("Conclusión") and "hallazgo 0" not in out and "### [" not in out
    assert out.rstrip().endswith(rp.BOUNDED_NOTE)
    inv = orch.investigation()
    assert inv["turn"] == 1 and inv["agents"] == ["architecture"] and "hallazgo 129" in inv["text"]
    line = [p for p in orch.printed if "Investigación oculta" in p]
    assert len(line) == 1 and "architecture · 4 vueltas" in line[0] and "/detalle" in line[0]
    assert "citas verificadas" in line[0] and "sin respaldo" in line[0]
    # La memoria de la sesión conserva todo; el registro de métricas no lleva texto.
    assert "hallazgo 5" in orch.memory.turns[-1].result
    assert "hallazgo" not in json.dumps(orch.last_run_record)


def test_linea_resumen_visible_aunque_quiet(orch):
    orch.quiet = True
    orch.run("genera un reporte del flujo de compra")
    assert any("Investigación oculta" in p for p in orch.printed)


def test_sin_ocultar_imprime_todo_como_hoy(orch):
    orch.hide_investigation = False
    out = orch.run("genera un reporte del flujo de compra")
    assert f"### [{ARCH}]" in out and "hallazgo 0" in out and out.count("### [") == 2
    assert orch.investigations == [] and not any("Investigación oculta" in p for p in orch.printed)


def test_plan_de_un_agente_no_cambia(orch, monkeypatch):
    monkeypatch.setattr(planner, "_llm_plan", lambda *a, **k: planner.LLMPlan([{"agent": ARCH, "task": "t"}], None, None, 0, None))
    out = orch.run("explora el gateway: ¿qué capas intervienen? ¿qué builders arman cada operación?")
    assert "hallazgo 0" in out and orch.investigations == []


def test_detalle_por_turno_y_clear(orch):
    orch.run("genera un reporte del flujo de compra")
    orch.run("otra cosa: genera un reporte del reverso")
    assert [inv["turn"] for inv in orch.investigations] == [1, 2]
    assert orch.investigation(1)["turn"] == 1 and orch.investigation(9) is None
    orch.clear_session()
    assert orch.investigation() is None and len(orch.memory) == 0


def test_perfil_decision_en_el_ultimo_paso(orch):
    orch.run("genera un reporte del flujo de compra")
    assert orch.fake_agents[ARCH].profiles == [rp.PROFILE_FULL]
    assert orch.fake_agents[REPORT].profiles == [rp.PROFILE_DECISION]


# ---------------------------------------------------------------------------
# REPL: /detalle, /detalle N, paginación, /clear; flags de la CLI
# ---------------------------------------------------------------------------


@pytest.fixture
def repl(orch, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-de-prueba")
    monkeypatch.setattr(minka_main, "_make_orchestrator", lambda: orch)
    monkeypatch.setattr(minka_main, "_is_tty", lambda: True)  # oculta la investigación…
    reader = ReplReader(minka_main.console, tty=False, collapse_lines=3, collapse_chars=500, warn_tokens=2000)
    monkeypatch.setattr(minka_main, "_make_reader", lambda: reader)  # …pero lee con input()
    return orch


def test_repl_detalle_pagina_y_clear(repl, monkeypatch):
    entrada = "genera un reporte del flujo\n/detalle\n\nq\n/detalle 1\n\n\n/detalle 9\n/detalle x\n/clear\n/detalle\nsalir\n"
    result = CliRunner().invoke(minka_main.app, [], input=entrada)
    assert result.exit_code == 0, result.output
    out = result.output
    assert out.count("Investigación del pedido 1") == 2
    assert "60/131 líneas" in out and "120/131 líneas" in out  # 130 líneas + cabecera, bloques de 60
    assert out.count("hallazgo 129") == 1  # la primera lectura salió con q; la segunda llegó al final
    assert "No hay investigación oculta" in out and out.count("No hay investigación") == 2
    assert "Uso: /detalle" in out


def test_repl_sin_tty_imprime_todo(orch, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-de-prueba")
    monkeypatch.setattr(minka_main, "_make_orchestrator", lambda: orch)
    monkeypatch.setattr(minka_main, "_is_tty", lambda: False)
    result = CliRunner().invoke(minka_main.app, [], input="genera un reporte del flujo\nsalir\n")
    assert result.exit_code == 0, result.output
    assert orch.hide_investigation is False and "hallazgo 0" in result.output


@pytest.mark.parametrize(
    "tty,kwargs,esperado",
    [
        (True, {}, True),
        (False, {}, False),
        (True, {"completo": True}, False),
        (True, {"debug": True}, False),
        (True, {"plain": True}, False),
        (True, {"json_out": True}, False),
    ],
)
def test_hide_investigation_por_flags(monkeypatch, tty, kwargs, esperado):
    monkeypatch.setattr(minka_main, "_is_tty", lambda: tty)
    monkeypatch.delenv("MINKA_PLAIN", raising=False)
    base = {"completo": False, "debug": False, "plain": False, "json_out": False}
    assert minka_main._hide_investigation(**{**base, **kwargs}) is esperado


def test_minka_plain_env_imprime_todo(monkeypatch):
    monkeypatch.setattr(minka_main, "_is_tty", lambda: True)
    monkeypatch.setenv("MINKA_PLAIN", "1")
    assert minka_main._hide_investigation(completo=False, debug=False, plain=False, json_out=False) is False


# ---------------------------------------------------------------------------
# perfil decisión por ruta
# ---------------------------------------------------------------------------


def test_profile_decision_por_ruta():
    assert rp.profile_for("llm", REPORT, plan_agents=[ARCH, REPORT], last_of_plan=True) == rp.PROFILE_DECISION
    assert rp.profile_for("llm", "debug_agent", plan_agents=[ARCH, "debug_agent"], last_of_plan=True) == rp.PROFILE_DECISION
    assert rp.profile_for("llm", ARCH, plan_agents=[ARCH, REPORT], last_of_plan=False) == rp.PROFILE_FULL
    assert rp.profile_for("llm", REPORT, plan_agents=[REPORT], last_of_plan=True) == rp.PROFILE_REPORT
    assert rp.profile_for("llm", REPORT, force_full=True, plan_agents=[ARCH, REPORT], last_of_plan=True) == rp.PROFILE_REPORT
    assert rp.profile_for("llm", "debug_agent", force_full=rp.wants_full("en detalle"), plan_agents=[ARCH, "debug_agent"], last_of_plan=True) == rp.PROFILE_FULL
    assert rp.profile_for("llm", ARCH, plan_agents=[ARCH]) == rp.PROFILE_EXPLORE
    block = rp.user_block(rp.PROFILE_DECISION)
    for texto in ("Conclusión", "Hallazgos clave", "Riesgos", "Qué falta decidir", "30 a 50 líneas", "sin evidencia", "no añade datos"):
        assert texto in block
    assert "no añade datos" not in rp.user_block(rp.PROFILE_EXPLORE)
    assert rp.with_bounded_note("t", rp.PROFILE_DECISION).endswith(rp.BOUNDED_NOTE)


def test_citas_intactas_en_respuesta_decision():
    reply = "Conclusión: ok.\n\nHallazgos clave\n- `ResolveToken()` en pos/token.go:2\n\n¿Quieres que siga?"
    out, offered, _ = rp.strip_closing_offer(reply)
    assert offered and "pos/token.go:2" in out and "`ResolveToken()`" in out


# ---------------------------------------------------------------------------
# verificador: "(doc)", mayúsculas y sugerencia de basename
# ---------------------------------------------------------------------------


@pytest.fixture
def index(tmp_path):
    _git_repo(tmp_path / "pos", {"key_activation.go": "package k\ntype Keys struct { WorkingKey string }\n"})
    return RepoIndex({"pos": str(tmp_path / "pos")})


def test_identificador_solo_en_knowledge_se_marca_doc(index):
    r = cc.verify("El campo `checkDigits` y `http_gateway` vienen del manual.", index, previous=[],
                  docs="… el campo checkDigits del request … componente http_gateway …")
    assert (r.identifiers_unsupported, r.doc_supported) == (0, 2)
    assert r.text.endswith("(doc: checkDigits, http_gateway)") and cc.MARK_UNSUPPORTED not in r.text


def test_identificador_con_otra_capitalizacion_cuenta_como_codigo(index):
    r = cc.verify("Se usa `workingKey` al cifrar.", index, previous=[], docs="")
    assert r.identifiers_unsupported == 0 and r.doc_supported == 0 and r.text == "Se usa `workingKey` al cifrar."


def test_sin_repos_ni_doc_sigue_sin_respaldo(index):
    r = cc.verify("Ver `Fantasma_z()`.", index, previous=[], docs="nada relacionado")
    assert r.identifiers_unsupported == 1 and cc.MARK_UNSUPPORTED in r.text


def test_bloque_respaldado_solo_por_doc(index):
    r = cc.verify("```go\nx := checkDigits_calc()\n```", index, previous=[], docs="checkDigits_calc")
    assert r.blocks_unsupported == 0 and r.doc_supported == 1 and cc.MARK_DOC_BLOCK in r.text


def test_load_project_docs_lee_extracted_y_learning(tmp_path):
    for sub in ("extracted", "learning", "team"):
        (tmp_path / sub).mkdir()
    (tmp_path / "extracted" / "a.md").write_text("alfa")
    (tmp_path / "learning" / "b.md").write_text("beta")
    (tmp_path / "team" / "c.md").write_text("gamma")
    docs = cc.load_project_docs(tmp_path)
    assert "alfa" in docs and "beta" in docs and "gamma" not in docs
    assert cc.load_project_docs(None) == "" and cc.load_project_docs(tmp_path / "no") == ""


def test_cita_inexistente_sugiere_basename_parecido(index):
    r = citation_check.verify("Se activa en activatePek.go:3 y en pos/key_activation.go:2.", index)
    assert r.missing == 1
    assert "activatePek.go:3 ⚠ no existe (¿quisiste decir pos/key_activation.go?)" in r.text
    assert "pos/key_activation.go:2 ⚠" not in r.text


def test_sugerencia_solo_si_hay_parecido(index):
    r = citation_check.verify("Mira zzz_totalmente_otro.go:1.", index)
    assert r.missing == 1 and "quisiste decir" not in r.text
    assert index.suggest("keyActivation.go") == ["pos/key_activation.go"]
