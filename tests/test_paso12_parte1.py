"""Paso 12, parte 1 — que minka no afirme de más. Sin red: agentes y planner falsos, repos git
y knowledge temporales. Cubre: uso literal de /mal, consistencia solo entre corridas, saludo +
texto, estimado por (ruta, pasos), ids R-nn contra el catálogo, procedencia de duraciones y
que `evals quality` puntúa igual con las marcas nuevas."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from minka import main as minka_main
from minka.config_loader import config
from minka.core import metrics
from minka.core.evaluator import quality_evals as qe
from minka.core.evaluator.regression_evals import EvalCase
from minka.core.orchestrator import claim_check as cc
from minka.core.orchestrator import planner, router
from minka.core.orchestrator.orchestrator import AGENT_NAMES, Orchestrator
from minka.core.tools.analysis.citations import RepoIndex

ARCH = "architecture_agent"
GO = (
    "package main\n\nimport \"time\"\n\n// ResolveToken renueva el token; timeout 30s\n"
    "func ResolveToken() time.Duration {\n\treturn 5 * time.Minute\n}\n\nconst SessionTTL = 1800\n"
)
CATALOG = "# Catálogo de riesgos\n\n" + "\n".join(f"## R-{i:02d} — riesgo {i}" for i in range(1, 14)) + "\n"
NOTAS = "# Notas\n\nEl timeout del cliente es 300 s según el manual.\nLa reunión semanal dura 45 min.\n"


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "pos"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "t")
    (r / "main.go").write_text(GO)
    _git(r, "add", ".")
    _git(r, "commit", "-q", "-m", "init")
    return r


@pytest.fixture
def knowledge(tmp_path):
    k = tmp_path / "knowledge"
    (k / "extracted").mkdir(parents=True)
    (k / "learning").mkdir()
    (k / "extracted" / "risk_catalog.md").write_text(CATALOG)
    (k / "extracted" / "notas.md").write_text(NOTAS)
    return k


# ---------------------------------------------------------------------------
# 1. /mal muestra los corchetes
# ---------------------------------------------------------------------------


def test_uso_de_mal_muestra_los_corchetes_literalmente():
    orch = SimpleNamespace(last_run_record={}, memory=SimpleNamespace(turns=[]))
    with minka_main.console.capture() as cap:
        minka_main._mark_bad_routing(orch, "")
    out = cap.get()
    assert "/mal [agente_esperado] [nota hasta 80 caracteres]" in out


# ---------------------------------------------------------------------------
# 2. inconsistencia solo entre corridas distintas
# ---------------------------------------------------------------------------

FIRST = "El token se resuelve en `ResolveToken()` con timeout 30 segundos (pos/token.go:12)."
OTHER = "El token se resuelve en `FetchToken()` con timeout 45 segundos (pos/token.go:12)."


def test_consistency_memory_ignora_los_pasos_del_mismo_run():
    mem = cc.ConsistencyMemory()
    q = "¿dónde se resuelve el token?"
    r1 = cc.verify(FIRST, RepoIndex({}), previous=[FIRST], consistency=mem, question=q, run_id="run-1")
    r2 = cc.verify(OTHER, RepoIndex({}), previous=[OTHER], consistency=mem, question=q, run_id="run-1")
    assert r1.claims_inconsistent == 0 and r2.claims_inconsistent == 0 and cc.MARK_INCONSISTENT not in r2.text
    r3 = cc.verify(FIRST, RepoIndex({}), previous=[FIRST], consistency=mem, question=q, run_id="run-2")
    assert r3.claims_inconsistent == 1 and r3.text.rstrip().endswith(cc.MARK_INCONSISTENT)
    mem.clear()
    assert cc.verify(OTHER, RepoIndex({}), previous=[OTHER], consistency=mem, question=q, run_id="run-3").claims_inconsistent == 0


class _FakeAgent:
    def __init__(self, name, reply):
        self.name, self.reply = name, reply
        self.last_usage = {"agent": name, "model": "claude-haiku-4-5", "requests": 1, "input_tokens": 10,
                           "output_tokens": 5, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
                           "cache_creation_5m": 0, "cache_creation_1h": 0}

    def run(self, task):
        return self.reply


@pytest.fixture
def orch(tmp_path, monkeypatch):
    for key, value in (
        ("USE_BEDROCK", False), ("EVALUATOR_ENABLED", False), ("META_AGENT_ENABLED", False),
        ("OUTPUT_DIR", tmp_path / "out"), ("CITATION_CHECK_ENABLED", False), ("REPOS", {}),
        ("SESSION_MAX_USD", 100.0), ("BURST_GAP_SECONDS", 0),
    ):
        monkeypatch.setattr(config, key, value, raising=False)
    o = Orchestrator()
    o.quiet = True
    o.route_medians_fn = lambda: {}
    return o


def test_plan_de_dos_pasos_cero_inconsistentes_y_corridas_separadas_si(orch, monkeypatch):
    steps = [{"agent": ARCH, "task": "t"}, {"agent": "report_agent", "task": "r"}]
    monkeypatch.setattr(planner, "_llm_plan", lambda *a, **k: planner.LLMPlan(steps))
    replies = {ARCH: FIRST, "report_agent": OTHER}
    monkeypatch.setattr(orch, "_get_agent", lambda name: _FakeAgent(name, replies[name]))
    pedido = "analiza el flujo del token y luego genera un reporte"
    orch.run(pedido)
    assert orch.last_plan == [ARCH, "report_agent"]
    assert orch.last_run_record["claims_inconsistent"] == 0
    # Segunda corrida de la misma pregunta con otros valores en la misma cita: sí se marca.
    orch.run(pedido)
    assert orch.last_run_record["claims_inconsistent"] >= 1


# ---------------------------------------------------------------------------
# 3. saludo + texto
# ---------------------------------------------------------------------------


def test_strip_greeting_y_has_task_signal():
    assert router.strip_greeting("hola, ¿dónde se resuelve el token?") == "¿dónde se resuelve el token?"
    assert router.strip_greeting("buenas, retomemos lo de ayer") == "retomemos lo de ayer"
    assert router.strip_greeting("hola") is None and router.strip_greeting("¿dónde está?") is None
    assert router.has_task_signal("retomemos lo de ayer") is False
    assert router.has_task_signal("¿dónde se resuelve el token?") is True
    assert router.has_task_signal("revisa el diff") is True


@pytest.mark.parametrize(
    ("pedido", "esperado"),
    [
        ("hola minka, estoy de vuelta", "ambiguous"),
        ("buenas, retomemos lo de ayer", "ambiguous"),
        ("hola, ¿dónde se resuelve el token?", "info_direct"),
        ("hola", "small_talk"),
        ("hola, ¿qué puedes hacer?", "meta"),
    ],
)
def test_saludo_mas_texto_se_enruta_por_el_resto(pedido, esperado):
    decision = router.local_decision(pedido, [], AGENT_NAMES, {"pos": "/tmp/pos"})
    assert decision is not None and decision.router == esperado
    if esperado == "ambiguous":
        assert decision.reply == router.ASK_AMBIGUOUS and decision.confidence == "baja"


def test_saludo_con_tarea_sigue_al_planner():
    assert router.local_decision("hola, implementa el retry en internal/x.go", [], AGENT_NAMES, {}) is None


# ---------------------------------------------------------------------------
# 4. estimado por (ruta, nº de pasos)
# ---------------------------------------------------------------------------


def test_route_median_counts_por_pasos(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(metrics, "cost_usd", lambda u: 0.2)
    monkeypatch.setattr(metrics, "baseline_usd", lambda u: 0.2)
    usage = {"per_agent": [{"agent": ARCH, "model": "m", "requests": 1}], "totals": {}}
    base = dict(run_id="r", source="repl", project="demo", cwd="x", fix_loop_iters=0, ok=True, duration_s=1.0, usage=usage)
    for plan in ([ARCH], [ARCH], [ARCH, "report_agent"]):
        metrics.write_run_record(metrics.build_run_record(**base, router="llm", plan=plan))
    flat = metrics.route_median_counts()
    by_steps = metrics.route_median_counts(by_steps=True)
    assert flat["llm"][1] == 3 and by_steps["llm/1"][1] == 2 and by_steps["llm/2"][1] == 1
    assert metrics.route_key("llm", 2) == "llm/2"


def test_estimado_usa_la_clave_ruta_pasos(orch, monkeypatch):
    lines = []
    monkeypatch.setattr(orch, "_stderr", SimpleNamespace(print=lambda text, **kw: lines.append(text)))
    orch.quiet = False
    steps = [{"agent": ARCH, "task": "t"}, {"agent": "report_agent", "task": "r"}]
    monkeypatch.setattr(planner, "_llm_plan", lambda *a, **k: planner.LLMPlan(steps))
    monkeypatch.setattr(orch, "_get_agent", lambda name: _FakeAgent(name, "ok"))
    orch.route_medians_fn = lambda: {"llm": (0.80, 12), "llm/1": (0.10, 30)}  # sin datos de 2 pasos
    orch.run("analiza el flujo de pagos y luego genera un reporte")
    assert "sin estimado" in lines[-1]
    orch._route_medians = None
    orch.route_medians_fn = lambda: {"llm/2": (0.30, 7)}
    orch.run("analiza el flujo de cobros y luego genera un reporte")
    assert "estimado ~$0.30 (mediana de 7 corridas)" in lines[-1]


# ---------------------------------------------------------------------------
# 5. ids R-nn contra el catálogo
# ---------------------------------------------------------------------------


def test_riesgos_contra_el_catalogo(knowledge):
    ids = cc.load_risk_ids(knowledge)
    assert ids == {f"R-{i:02d}" for i in range(1, 14)}
    text = "Aplican R-05 y R-14.\nTambién R-15, R-16 y R-17.\nSin ids aquí."
    r = cc.verify(text, RepoIndex({}), previous=[text], risk_ids=ids)
    lines = r.text.split("\n")
    assert lines[0].endswith(f"{cc.MARK_DOC_RISK} {cc.MARK_RISK_MISSING}: R-14")
    assert lines[1].endswith(f"{cc.MARK_RISK_MISSING}: R-15, R-16, R-17") and cc.MARK_DOC_RISK not in lines[1]
    assert lines[2] == "Sin ids aquí."
    assert (r.risks_checked, r.risks_unsupported) == (5, 4)
    # sin catálogo en el proyecto: no se marca nada
    assert cc.load_risk_ids(knowledge.parent / "no") is None
    assert cc.verify(text, RepoIndex({}), previous=[text], risk_ids=None).text == text


def test_riesgos_llegan_al_registro_y_a_stats(orch, knowledge, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_DIR", knowledge.parent.parent, raising=False)
    monkeypatch.setattr(config, "ACTIVE_PROJECT", knowledge.parent.name, raising=False)
    # knowledge vive en <PROJECTS_DIR>/<proyecto>/knowledge: aquí tmp_path/<tmpdir>/knowledge
    monkeypatch.setattr(orch, "_get_agent", lambda name: _FakeAgent(name, "El riesgo R-14 aplica al token (R-03 también)."))
    orch.run("¿dónde se resuelve el token?")
    assert orch.last_run_record["risks_unsupported"] == 1
    result = CliRunner().invoke(minka_main.app, ["stats"])
    assert result.exit_code == 0 and "riesgos sin catálogo: 1" in result.output


# ---------------------------------------------------------------------------
# 6. duraciones: patrón y procedencia
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("texto", ["300 s", "1800 s", "5 min", "2.5 h", "30s", "5m", "1h", "3 días", "2 semanas", "250 ms"])
def test_patron_de_duraciones(texto):
    assert texto in cc._numbers_in(f"espera {texto} antes", today="")


def test_patron_no_captura_lo_que_no_es_duracion():
    for texto in ("HTTP 200", "versión 1.2", "la 5a vez", "2026-11-13"):
        assert not any(k == "plazo" for k, _ in cc._numbers_with_kind(texto, today=""))


def test_procedencia_de_duraciones_en_tres_niveles(repo, knowledge):
    index = RepoIndex({"pos": str(repo)})
    docs = cc.load_project_docs(knowledge)
    text = (
        "El timeout de red es 30s (pos/main.go:5).\n"
        "El manual dice 300 s para el cliente.\n"
        "La sesión caduca a los 1800 s.\n"
        "La reunión de arquitectura dura 45 min.\n"
    )
    r = cc.verify(text, index, previous=[], docs=docs)
    lines = r.text.split("\n")
    assert lines[0] == "El timeout de red es 30s (pos/main.go:5)."  # (código): sin marca
    assert lines[1].endswith("(solo knowledge: 300 s → extracted/notas.md:3)")
    assert lines[2].endswith(f"{cc.MARK_NO_SOURCE}: 1800 s")
    assert lines[3] == "La reunión de arquitectura dura 45 min."
    assert (r.durations_checked, r.durations_doc_only, r.durations_unsupported) == (3, 1, 1)
    assert r.numbers_unsupported == 1 and "1800 s" in r.unsupported_tokens


def test_evidence_acepta_str_y_lista():
    e = cc.Evidence(None, [], docs="línea uno\nvalor 300 s")
    assert e.support("300 s") == cc.SUPPORT_DOC and e.doc_where["300 s"] == "knowledge:2"
    e2 = cc.Evidence(None, [], docs=[("extracted/a.md", 7, "ttl 1800 s")])
    assert e2.support("1800 s") == cc.SUPPORT_DOC and e2.doc_where["1800 s"] == "extracted/a.md:7"
    assert e2.support("9 min") is None


# ---------------------------------------------------------------------------
# 7. evals quality puntúa igual con y sin marcas
# ---------------------------------------------------------------------------


def test_strip_marks_quita_solo_las_anotaciones():
    text = (
        "Hay 1 definición de `ResolveToken()` en pos/main.go:6, timeout 1800 s ⚠ sin fuente: 1800 s\n"
        "Plazo 300 s (solo knowledge: 300 s → extracted/notas.md:3)\n"
        "Aplican R-05 y R-14. (doc: risk_catalog.md) ⚠ id inexistente en el catálogo: R-14\n"
        "Ver `Foo_bar()` ⚠ sin respaldo: `Foo_bar()`"
    )
    clean = cc.strip_marks(text)
    assert clean == (
        "Hay 1 definición de `ResolveToken()` en pos/main.go:6, timeout 1800 s\n"
        "Plazo 300 s\nAplican R-05 y R-14.\nVer `Foo_bar()`"
    )


@pytest.mark.parametrize(
    ("tipo", "expect", "salida"),
    [
        ("count", {"grep": "ResolveToken", "n": 2}, "Hay 2 menciones en pos/main.go:6; el TTL es 1800s y el timeout de red 30s."),
        ("location", {"file": "main.go", "symbol": "ResolveToken"}, "Está en pos/main.go:6 (`ResolveToken()`), con TTL 1800 s. Riesgo R-14."),
        ("existence", {"grep": "SessionTTL", "term": "SessionTTL"}, "Sí existe `SessionTTL` en pos/main.go:10, vale 1800 s (R-05, R-14)."),
    ],
)
def test_evals_quality_puntua_igual_con_marcas(repo, knowledge, tipo, expect, salida):
    case = EvalCase(id="c", input="¿?", type=tipo, expect=expect)
    repos = {"pos": str(repo)}
    before = qe.score(case, salida, repos)
    verified = cc.verify(
        salida, RepoIndex(repos), previous=[], docs=cc.load_project_docs(knowledge), risk_ids=cc.load_risk_ids(knowledge)
    )
    assert verified.text != salida  # sí hubo marcas nuevas
    assert cc.MARK_NO_SOURCE in verified.text and (cc.MARK_RISK_MISSING in verified.text or tipo == "count")
    after = qe.score(case, verified.text, repos)
    assert (after.verdict, after.note) == (before.verdict, before.note) and before.verdict == qe.VERDICT_OK
