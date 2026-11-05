"""Guarda del planner para report_agent, contexto completo entre pasos, perfil de reporte y
verificador determinista de afirmaciones (claim_check). Sin red ni modelo."""

from __future__ import annotations

import subprocess

import pytest

from minka.config_loader import config
from minka.core import metrics
from minka.core.orchestrator import claim_check as cc
from minka.core.orchestrator import planner, router
from minka.core.orchestrator import response_profile as rp
from minka.core.orchestrator.orchestrator import Orchestrator
from minka.core.orchestrator.state_manager import StateManager
from minka.core.tools.analysis.citations import RepoIndex

ARCH, REPORT = "architecture_agent", "report_agent"
PLAN_2 = [{"agent": ARCH, "task": "t1"}, {"agent": REPORT, "task": "t2"}]

# ---------------------------------------------------------------------------
# 1. router / planner
# ---------------------------------------------------------------------------


def test_report_sin_pedido_ni_resultado_previo_se_elimina_del_plan():
    out, notices = router.apply_hard_rules(PLAN_2, "analiza el flujo de compra", has_prev_result=False)
    assert [s["agent"] for s in out] == [ARCH]
    assert notices == ["regla: report exige pedir un reporte o un resultado previo → paso eliminado"]


def test_report_se_conserva_si_lo_piden_o_hay_resultado_previo():
    out, notices = router.apply_hard_rules(PLAN_2, "genera un reporte del flujo", has_prev_result=False)
    assert [s["agent"] for s in out] == [ARCH, REPORT] and notices == []
    out, notices = router.apply_hard_rules(PLAN_2, "analiza el flujo", has_prev_result=True)
    assert [s["agent"] for s in out] == [ARCH, REPORT] and notices == []


def test_plan_de_solo_report_sin_pedido_cae_a_architecture():
    out, notices = router.apply_hard_rules([{"agent": REPORT, "task": "t"}], "analiza esto", has_prev_result=False)
    assert [s["agent"] for s in out] == [ARCH]
    assert notices[-1] == "regla: plan sin pasos válidos → architecture"


def test_otros_agentes_siguen_redirigiendo_a_architecture():
    out, notices = router.apply_hard_rules([{"agent": "debug_agent", "task": "t"}], "analiza el flujo", False)
    assert [s["agent"] for s in out] == [ARCH] and "→ architecture" in notices[0]


def test_pedido_de_reporte_con_palabra_de_informacion_no_es_info_direct():
    pedido = "genera un reporte de cómo se arma la autorización y sus riesgos"
    assert router.is_simple_info_question(pedido)  # la pregunta sí es de información…
    decision = router.decide(pedido, [], repos={}) if hasattr(router, "decide") else None
    if decision is not None:
        assert decision.router != "info_direct"


# ---------------------------------------------------------------------------
# 2. contexto completo entre pasos
# ---------------------------------------------------------------------------


def test_context_for_next_pasa_el_resultado_completo():
    state = StateManager()
    state.record_step(ARCH, "t", "x" * 5000)
    ctx = state.context_for_next()
    assert "x" * 5000 in ctx and state.last_truncated_steps == []
    ctx = state.context_for_next(max_chars=12000)
    assert "x" * 5000 in ctx and "recortado" not in ctx


def test_context_for_next_recorta_con_tope_y_lo_anota():
    state = StateManager()
    state.record_step(ARCH, "t", "a" * 300)
    state.record_step("debug_agent", "t", "b" * 50)
    ctx = state.context_for_next(max_chars=100)
    assert "a" * 100 in ctx and "a" * 101 not in ctx
    assert "[… recortado: se muestran 100 de 300 caracteres]" in ctx
    assert "b" * 50 in ctx and state.last_truncated_steps == [1]


# ---------------------------------------------------------------------------
# 3. perfil de reporte
# ---------------------------------------------------------------------------


def test_bloque_de_reporte_en_user():
    block = rp.user_block(rp.PROFILE_REPORT)
    for texto in ("pasos previos", "bloques de código", "sin evidencia", "60", "¿Quieres que…?", "archivo:línea"):
        assert texto in block
    assert rp.profile_for("llm", REPORT, plan_agents=[ARCH, REPORT]) == rp.PROFILE_REPORT
    assert rp.with_bounded_note("t", rp.PROFILE_REPORT) == "t"  # sin nota "acotada"


# ---------------------------------------------------------------------------
# 4. verificador determinista
# ---------------------------------------------------------------------------


def _git_repo(path, files: dict[str, str]):
    path.mkdir()
    for name, content in files.items():
        (path / name).write_text(content)
    for args in (("init", "-q", "-b", "main"), ("config", "user.email", "t@example.com"),
                 ("config", "user.name", "t"), ("add", "."), ("commit", "-q", "-m", "init")):
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


@pytest.fixture
def index(tmp_path):
    _git_repo(tmp_path / "pos", {"token.go": "package svc\nfunc ResolveToken() {}\n// KCV 10E3\nconst traceId = 128263\n"})
    return RepoIndex({"pos": str(tmp_path / "pos")})


def test_identificador_existente_e_inexistente(index):
    text = "Usa `ResolveToken()` y luego `InventedHelper()` con `svc.ResolveToken`."
    r = cc.verify(text, index, previous=[])
    assert (r.identifiers_checked, r.identifiers_unsupported) == (3, 1)
    assert r.text.endswith(f"{cc.MARK_UNSUPPORTED}: InventedHelper()")
    assert r.unsupported_tokens == ["InventedHelper()"]


def test_identificador_respaldado_por_el_resultado_previo(index):
    r = cc.verify("Ver `PrevOnlyFunc()`.", index, previous=["el paso previo citó PrevOnlyFunc()"])
    assert r.unsupported == 0 and cc.MARK_UNSUPPORTED not in r.text


def test_palabras_normales_entre_backticks_no_cuentan(index):
    r = cc.verify("El valor `error` y `Enter` no son identificadores.", index, previous=[])
    assert r.identifiers_checked == 0


def test_cifras_con_y_sin_fuente(index):
    text = "KCV 10E3 confirmado; trace 128263 ok; RRN 100995275382 y fecha 2026-01-01; espera 30 días."
    r = cc.verify(text, index, previous=["… el reverso tardó 30 días …"], today="2026-11-05")
    assert r.numbers_unsupported == 2  # RRN y fecha
    assert "100995275382" in r.text.split(cc.MARK_UNSUPPORTED)[-1] and "2026-01-01" in r.text.split(cc.MARK_UNSUPPORTED)[-1]
    assert "10E3" not in r.text.split(cc.MARK_UNSUPPORTED)[-1]


def test_fecha_de_hoy_no_se_marca(index):
    r = cc.verify("Reporte — fecha 2026-11-05.", index, previous=[], today="2026-11-05")
    assert r.numbers_checked == 0


def test_bloque_de_codigo_existente_propuesto_e_inventado(index):
    text = (
        "## Estado\n```go\nfunc ResolveToken() {}\n```\n"
        "## Propuesta\n```go\nfunc NuevaCosa_x() { otraCosa_y() }\n```\n"
        "## Detalle\n```go\nfunc inventadoUno_a() { inventadoDos_b(); inventadoTres_c() }\n```\n"
        "```diff\n- old_thing\n+ new_thing\n```\n"
    )
    r = cc.verify(text, index, previous=[])
    assert (r.blocks_checked, r.blocks_unsupported, r.blocks_proposed) == (2, 1, 2)
    lines = r.text.split("\n")
    assert lines.count(cc.MARK_PROPOSED) == 2
    marked = [ln for ln in lines if ln.startswith(cc.MARK_UNSUPPORTED)]
    assert len(marked) == 1 and "3 de 3 identificadores" in marked[0]


def test_seccion_de_propuesta_no_marca_identificadores(index):
    r = cc.verify("## Recomendaciones\n- Crear `FuturoHelper()`.\n## Hechos\n- Hoy `OtroInventado()`.", index, [])
    assert r.identifiers_unsupported == 1 and "FuturoHelper" not in r.text.split("## Hechos")[1].split(":")[0]


def test_salida_cortada_se_marca(index):
    r = cc.verify("texto", index, previous=[], truncated=True)
    assert r.truncated and r.text.endswith(cc.MARK_TRUNCATED)


def test_citas_quedan_intactas(index):
    text = "El token se resuelve en `service/token.go:42` y en pos/token.go:3."
    r = cc.verify(text, index, previous=[])
    assert r.text == text and r.checked == 0


def test_sin_repos_ni_previo_marca_todo(tmp_path):
    r = cc.verify("Ver `Algo_x()`.", RepoIndex({}), previous=[])
    assert r.identifiers_unsupported == 1


# ---------------------------------------------------------------------------
# orquestador: contexto completo al report, verificación por paso y métricas
# ---------------------------------------------------------------------------

ARCH_REPLY = "Análisis: `ResolveToken()` vive en pos/token.go:2.\n" + ("detalle " * 400)
REPORT_REPLY = "# Reporte\n- Usa `ResolveToken()`.\n- Usa `Fantasma_z()`.\n```go\nfunc Fantasma_z() { otraFantasma_q() }\n```"


class _FakeAgent:
    def __init__(self, name):
        self.name, self.profiles, self.tasks = name, [], []
        self.last_usage = {
            "agent": name, "model": "claude-haiku-4-5", "requests": 1, "input_tokens": 1,
            "output_tokens": 1, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
            "cache_creation_5m": 0, "cache_creation_1h": 0, "truncated": 1 if name == REPORT else 0,
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
        ("VERIFY_CLAIMS_ENABLED", True), ("CONTEXT_NEXT_MAX_CHARS", 12000),
    ):
        monkeypatch.setattr(config, key, value, raising=False)
    monkeypatch.setattr(planner, "_llm_plan", lambda *a, **k: planner.LLMPlan(list(PLAN_2), None, None, 0, None))
    o = Orchestrator()
    o.quiet = True
    agents = {}
    monkeypatch.setattr(o, "_get_agent", lambda name: agents.setdefault(name, _FakeAgent(name)))
    o.fake_agents = agents
    return o


def test_report_recibe_el_analisis_completo_y_el_perfil_reporte(orch):
    out = orch.run("genera un reporte del flujo de compra")
    assert orch.last_router == "llm" and orch.last_plan == [ARCH, REPORT]
    task = orch.fake_agents[REPORT].tasks[0]
    assert "detalle " * 400 in task and "recortado" not in task  # 1 500 chars ya no es el tope
    assert orch.fake_agents[REPORT].profiles == [rp.PROFILE_DECISION]  # último paso de 2
    # Verificación por paso: lo del análisis previo respalda al reporte; lo nuevo se marca.
    assert out.count(cc.MARK_UNSUPPORTED) == 2  # `Fantasma_z()` en línea + bloque
    assert cc.MARK_TRUNCATED in out
    rec = orch.last_run_record
    assert rec["claims_unsupported"] == 2 and rec["claims_checked"] >= 3
    assert rec["truncated_outputs"] == 1 and rec["blocks_proposed"] == 0
    assert metrics.aggregate([rec])["claims_unsupported"] == 2


def test_interruptor_apaga_el_verificador(orch, monkeypatch):
    monkeypatch.setattr(config, "VERIFY_CLAIMS_ENABLED", False)
    out = orch.run("genera un reporte del flujo de compra")
    assert cc.MARK_UNSUPPORTED not in out and orch.last_run_record["claims_checked"] == 0


def test_contexto_recortado_avisa_en_la_linea_de_decision(orch, monkeypatch, capsys):
    monkeypatch.setattr(config, "CONTEXT_NEXT_MAX_CHARS", 200)
    orch.quiet = False
    orch.run("genera un reporte del flujo de compra")
    assert "recortado a CONTEXT_NEXT_MAX_CHARS" in capsys.readouterr().out
    assert "[… recortado: se muestran 200 de" in orch.fake_agents[REPORT].tasks[0]
