"""Lote 5: tope de sesión, freno de ráfaga y estimado previo (core/orchestrator/budget.py).
Sin red: agentes falsos, planner falso, reloj falso y precio fijo por usage."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from minka import main as minka_main
from minka._app import SOURCE_ENV
from minka.config_loader import config
from minka.core import metrics
from minka.core.orchestrator import budget, planner
from minka.core.orchestrator.orchestrator import Orchestrator

ARCH = "architecture_agent"
PREGUNTA = "¿dónde se resuelve el token?"
PLAN = "analiza el flujo de pagos y genera un reporte de riesgos"
USAGE = {"model": "claude-haiku-4-5", "requests": 1, "input_tokens": 10, "output_tokens": 5,
         "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_5m": 0,
         "cache_creation_1h": 0}


class _FakeAgent:
    def __init__(self, name, interrupted=0):
        self.name = name
        self.last_usage = {"agent": name, **USAGE, "interrupted": interrupted}

    def run(self, task):
        return f"resultado de {self.name} en internal/x.go:1"


@pytest.fixture
def orch(tmp_path, monkeypatch):
    for key, value in (
        ("USE_BEDROCK", False), ("EVALUATOR_ENABLED", False), ("META_AGENT_ENABLED", False),
        ("OUTPUT_DIR", tmp_path / "out"), ("CITATION_CHECK_ENABLED", False), ("REPOS", {}),
        ("SESSION_MAX_USD", 100.0), ("BURST_N", 3), ("BURST_GAP_SECONDS", 5), ("ESTIMATE_CONFIRM_USD", 0.50),
        ("ESTIMATE_MIN_RUNS", 5),
    ):
        monkeypatch.setattr(config, key, value, raising=False)
    monkeypatch.delenv(SOURCE_ENV, raising=False)
    # Precio fijo: $0.15 por usage (agente o planner), sin tarifa real.
    monkeypatch.setattr(metrics, "cost_usd", lambda u: 0.15)
    monkeypatch.setattr(metrics, "baseline_usd", lambda u: 0.15)
    state = SimpleNamespace(t=1000.0, llm_calls=0, lines=[], agents={})

    def fake_llm(task, context=""):
        state.llm_calls += 1
        return planner.LLMPlan([{"agent": ARCH, "task": "t"}, {"agent": "report_agent", "task": "r"}], None, None, 0, dict(USAGE))

    monkeypatch.setattr(planner, "_llm_plan", fake_llm)
    o = Orchestrator()
    o.budget.clock = lambda: state.t
    o.route_medians_fn = lambda: {}
    monkeypatch.setattr(o, "_get_agent", lambda name: state.agents.setdefault(name, _FakeAgent(name)))
    monkeypatch.setattr(o, "_stderr", SimpleNamespace(print=lambda text, **kw: state.lines.append(text)))
    state.orch = o
    return state


def _runs(state, n, gap):
    """n corridas info_direct separadas `gap` segundos (reloj falso). Devuelve en cuál frenó."""
    for i in range(1, n + 1):
        state.t += gap
        try:
            state.orch.run(f"{PREGUNTA[:-1]} {i}?")
        except budget.BudgetGate as gate:
            return i, gate
    return None, None


# ---------------------------------------------------------------------------
# ráfaga
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("gap", [0.0, 0.05, 0.1])
def test_nueve_corridas_pegadas_frenan_en_la_cuarta(orch, gap):
    stopped, gate = _runs(orch, 9, gap)
    assert stopped == 4 and gate.kind == budget.KIND_BURST and gate.n == 3
    assert "Ráfaga" in gate.line() and orch.orch.last_exit_code == budget.EXIT_CODE
    assert len(orch.agents[ARCH].last_usage) and orch.orch.last_run_record["ok"] is False


def test_tres_corridas_separadas_diez_segundos_no_frenan(orch):
    stopped, _ = _runs(orch, 3, 10.0)
    assert stopped is None and orch.orch.budget.burst == 0


def test_si_reinicia_la_rafaga_y_no_deja_el_contador(orch):
    stopped, gate = _runs(orch, 4, 0.0)
    assert stopped == 4
    orch.orch.budget.allow(gate)
    assert orch.orch.budget.burst == 0
    stopped, _ = _runs(orch, 3, 0.0)  # tres más pegadas pasan; la cuarta volvería a frenar
    assert stopped is None


# ---------------------------------------------------------------------------
# tope de sesión
# ---------------------------------------------------------------------------


def test_tope_030_con_015_por_corrida_frena_la_tercera(orch, monkeypatch):
    monkeypatch.setattr(config, "SESSION_MAX_USD", 0.30, raising=False)
    orch.orch.budget = budget.BudgetState(clock=lambda: orch.t)
    stopped, gate = _runs(orch, 3, 10.0)
    assert stopped == 3 and gate.kind == budget.KIND_SESSION
    assert gate.usd == pytest.approx(0.30) and gate.n == 2 and gate.ceiling == 0.30
    assert orch.orch.session_usd == pytest.approx(0.30)
    # un "sí" sube un escalón (otro SESSION_MAX_USD), no una sola corrida
    assert "0.60" in orch.orch.budget.allow(gate)
    stopped, gate = _runs(orch, 3, 10.0)  # dos corridas más caben; la tercera vuelve a frenar
    assert stopped == 3 and gate.kind == budget.KIND_SESSION and gate.ceiling == 0.60


def test_sesion_suma_planner_e_interrumpidas(orch, monkeypatch):
    orch.t += 10
    orch.orch.run(PLAN)  # planner (0.15) + architecture (0.15) + report (0.15)
    assert orch.llm_calls == 1 and orch.orch.last_run_record["cost_usd"] == pytest.approx(0.45)
    assert any(u.get("agent") == "planner" for u in orch.orch.last_run_record["per_agent"])
    assert orch.orch.session_usd == pytest.approx(0.45)
    orch.t += 10
    orch.agents.clear()
    monkeypatch.setattr(orch.orch, "_get_agent", lambda name: _FakeAgent(name, interrupted=1))
    orch.orch.run(PREGUNTA)  # parcial: no es turno útil, pero su costo cuenta
    assert orch.orch.last_run_ok is False and orch.orch.session_usd == pytest.approx(0.60)


def test_capas_de_costo_cero_nunca_frenan(orch, monkeypatch):
    monkeypatch.setattr(config, "SESSION_MAX_USD", 0.01, raising=False)
    orch.orch.budget = budget.BudgetState(clock=lambda: orch.t)
    orch.orch.budget.session_usd = 5.0
    for pedido in ("hola", "¿en qué quedamos?", "¿qué puedes hacer?", "eso"):
        orch.orch.run(pedido)  # sin BudgetGate
    with pytest.raises(budget.BudgetGate):
        orch.orch.run(PREGUNTA)


def test_bridge_y_eval_quality_no_pasan_por_el_gate(orch, monkeypatch):
    monkeypatch.setattr(config, "SESSION_MAX_USD", 0.01, raising=False)
    orch.orch.budget = budget.BudgetState(clock=lambda: orch.t)
    orch.orch.budget.session_usd = 5.0
    orch.orch.source = "eval_quality"  # lo que fija quality_evals.default_runner
    monkeypatch.setenv(SOURCE_ENV, "eval_quality")
    stopped, _ = _runs(orch, 9, 0.0)
    assert stopped is None
    monkeypatch.setenv(SOURCE_ENV, "bridge")
    orch.orch.run(PREGUNTA, forced_agent="architecture")


def test_forzado_tambien_pasa_por_el_gate(orch, monkeypatch):
    monkeypatch.setattr(config, "SESSION_MAX_USD", 0.01, raising=False)
    orch.orch.budget = budget.BudgetState(clock=lambda: orch.t)
    orch.orch.budget.session_usd = 5.0
    with pytest.raises(budget.BudgetGate):
        orch.orch.run(PREGUNTA, forced_agent="architecture")
    assert orch.agents == {}


# ---------------------------------------------------------------------------
# estimado previo
# ---------------------------------------------------------------------------


def test_estimado_en_la_linea_y_confirmacion_si_supera_050(orch):
    orch.orch.route_medians_fn = lambda: {"llm": (0.80, 12)}
    orch.t += 10
    with pytest.raises(budget.BudgetGate) as exc:
        orch.orch.run(PLAN)
    gate = exc.value
    assert gate.kind == budget.KIND_ESTIMATE and gate.usd == 0.80 and gate.n == 12
    assert "estimado ~$0.80 (mediana de 12 corridas)" in orch.lines[-1]
    assert orch.agents == {} and orch.llm_calls == 1
    # "sí": se relanza el mismo pedido sin volver a llamar al planner
    orch.orch.budget.allow(gate)
    orch.t += 10
    orch.orch.run(PLAN)
    assert orch.llm_calls == 1 and set(orch.agents) == {ARCH, "report_agent"}
    assert orch.orch.last_run_record["cost_usd"] == pytest.approx(0.30)  # sin el planner la 2ª vez


def test_sin_datos_suficientes_dice_sin_estimado_y_no_pregunta(orch):
    orch.orch.route_medians_fn = lambda: {"llm": (0.80, 4)}
    orch.t += 10
    orch.orch.run(PLAN)
    assert "sin estimado" in orch.lines[-1] and "~$" not in orch.lines[-1]


def test_estimado_bajo_el_umbral_no_pregunta_salvo_que_rompa_el_tope(orch, monkeypatch):
    orch.orch.route_medians_fn = lambda: {"llm": (0.20, 9)}
    orch.t += 10
    orch.orch.run(PLAN)
    assert "estimado ~$0.20 (mediana de 9 corridas)" in orch.lines[-1]
    monkeypatch.setattr(config, "SESSION_MAX_USD", 0.50, raising=False)
    orch.orch.budget = budget.BudgetState(clock=lambda: orch.t)
    orch.orch.budget.session_usd = 0.40
    orch.t += 10
    with pytest.raises(budget.BudgetGate) as exc:
        orch.orch.run(PLAN)
    assert exc.value.kind == budget.KIND_ESTIMATE
    assert "tope de sesión ahora $1.00" in orch.orch.budget.allow(exc.value)


def test_info_direct_inherit_y_capas_cero_sin_estimado(orch):
    orch.orch.route_medians_fn = lambda: {"info_direct": (0.10, 50), "inherit": (0.05, 9), "llm": (0.3, 9)}
    orch.t += 10
    orch.orch.run(PREGUNTA)
    assert "estimado" not in orch.lines[-1]
    orch.t += 10
    orch.orch.run("¿qué opinas?")  # hereda el turno previo
    assert orch.orch.last_router == "inherit" and "estimado" not in orch.lines[-1]
    orch.orch.run("hola")
    assert "estimado" not in orch.lines[-1]


def test_estimate_line_nunca_inventa_rangos():
    assert budget.estimate_line(None, 3) == "sin estimado"
    assert budget.estimate_line(0.4567, 7) == "estimado ~$0.46 (mediana de 7 corridas)"


# ---------------------------------------------------------------------------
# CLI / REPL
# ---------------------------------------------------------------------------


@pytest.fixture
def cli(orch, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-de-prueba")
    monkeypatch.setattr(minka_main, "_make_orchestrator", lambda: orch.orch)
    monkeypatch.setattr(minka_main, "_is_tty", lambda: False)
    monkeypatch.setattr(config, "SESSION_MAX_USD", 0.01, raising=False)
    orch.orch.budget = budget.BudgetState(clock=lambda: orch.t)
    orch.orch.budget.session_usd = 5.0
    return orch


def test_sin_tty_linea_clara_y_exit_3(cli):
    result = CliRunner().invoke(minka_main.app, [PREGUNTA])
    assert result.exit_code == budget.EXIT_CODE, result.output
    assert "Tope de sesión" in result.output and "exit 3" in result.output
    assert cli.agents == {}


def test_repl_sin_tty_sale_con_3(cli):
    result = CliRunner().invoke(minka_main.app, [], input=f"{PREGUNTA}\nsalir\n")
    assert result.exit_code == budget.EXIT_CODE and "Tope de sesión" in result.output


def test_bridge_en_cli_no_frena(cli, monkeypatch):
    monkeypatch.setenv(SOURCE_ENV, "bridge")
    result = CliRunner().invoke(minka_main.app, [PREGUNTA, "--agent", "architecture"])
    assert result.exit_code == 0, result.output


def test_con_tty_si_sube_el_tope_y_no_descarta(cli, monkeypatch):
    gate = budget.BudgetGate(budget.KIND_SESSION, 5.0, 3, 0.01)
    monkeypatch.setattr(minka_main.Confirm, "ask", staticmethod(lambda *a, **k: True))
    assert minka_main._resolve_budget_gate(cli.orch, gate, tty=True) is True
    assert cli.orch.budget.ceiling == pytest.approx(0.02)
    monkeypatch.setattr(minka_main.Confirm, "ask", staticmethod(lambda *a, **k: False))
    assert minka_main._resolve_budget_gate(cli.orch, gate, tty=True) is False
    assert cli.orch.budget.ceiling == pytest.approx(0.02)


def test_status_muestra_el_gasto_de_la_sesion(cli, monkeypatch):
    monkeypatch.setattr(config, "SESSION_MAX_USD", 100.0, raising=False)
    cli.orch.budget = budget.BudgetState(clock=lambda: cli.t)
    cli.orch.budget.session_usd = 1.23
    result = CliRunner().invoke(minka_main.app, [], input="/status\nsalir\n")
    assert result.exit_code == 0 and "$1.23 en la sesión" in result.output


def test_stats_y_doctor_avisan_del_tope_diario_sin_bloquear(tmp_path, monkeypatch):
    from minka.core import doctor

    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(config, "DAILY_MAX_USD", 0.10, raising=False)
    base = dict(run_id="r", source="repl", project="demo", cwd="x", router="info_direct", plan=[ARCH],
                fix_loop_iters=0, ok=True, duration_s=1.0)
    usage = {"per_agent": [{"agent": ARCH, **USAGE}], "totals": dict(USAGE)}
    monkeypatch.setattr(metrics, "cost_usd", lambda u: 0.15)
    monkeypatch.setattr(metrics, "baseline_usd", lambda u: 0.15)
    metrics.write_run_record(metrics.build_run_record(**base, usage=usage))
    metrics.write_run_record(metrics.build_run_record(**{**base, "source": "eval_quality"}, usage=usage))
    result = CliRunner().invoke(minka_main.app, ["stats"])
    assert result.exit_code == 0 and "DAILY_MAX_USD" in result.output and "$0.15" in result.output
    check = [c for c in doctor.run_checks(config) if c.name == "Gasto del día"][0]
    assert check.ok is False and check.critical is False and "$0.15 de $0.10" in check.detail
    assert doctor.has_critical_failure([check]) is False
    monkeypatch.setattr(config, "DAILY_MAX_USD", 50.0, raising=False)
    assert CliRunner().invoke(minka_main.app, ["stats"]).output.count("DAILY_MAX_USD") == 0
    assert [c for c in doctor.run_checks(config) if c.name == "Gasto del día"][0].ok is True
