"""`minka evals routing`: solo planner, con planner simulado; tasas, estabilidad, tokens, costo, --json-out."""

import json

from typer.testing import CliRunner

from minka import main as minka_main
from minka.core.evaluator import routing_evals
from minka.core.orchestrator import planner
from minka.core.structured import StructuredError

USAGE = {"model": "claude-sonnet-5", "requests": 1, "input_tokens": 1000, "output_tokens": 100,
         "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}


def test_info_cases_son_maximo_12_y_genericos():
    cases = routing_evals.info_cases()
    assert 1 <= len(cases) <= 12 and all(c.get("eval") == "info" for c in cases)
    raw = json.dumps(cases).lower()
    assert not any(w in raw for w in ("kushki", "credibanco", "amex", "usrv"))


def test_evaluate_calcula_tasas_y_costo(monkeypatch):
    respuestas = iter([
        planner.LLMPlan([{"agent": "architecture_agent", "task": "t"}], None, "ok", 0, USAGE),
        planner.LLMPlan(None, StructuredError("empty_plan", "", "sin pasos"), None, 0, USAGE),
        planner.LLMPlan(None, StructuredError("validation", "ValidationError", "x"), None, 1, USAGE),
        planner.LLMPlan([{"agent": "debug_agent", "task": "t"}], None, None, 0, USAGE),
    ])
    monkeypatch.setattr(planner, "_llm_plan", lambda task, context="": next(respuestas))
    cases = [
        {"id": "a", "request": "¿dónde está X?", "expect": {"agents": ["architecture_agent"]}},
        {"id": "b", "request": "¿cómo se valida Y?", "expect": {"agents": ["architecture_agent"]}},
    ]
    report = routing_evals.evaluate(cases, runs=2)
    assert report["calls"] == 4 and report["empty_plan_rate"] == 0.25
    assert report["planner_errors"] == {"validation": 1}
    assert report["agreement_rate"] == 0.25 and report["stability_rate"] == 0.0
    assert report["input_tokens"] == 4000 and report["output_tokens"] == 400
    assert report["cost_usd"] == round(4 * (1000 * 2.0 + 100 * 10.0) / 1e6, 4)
    assert report["per_case"][0]["agree"] == "1/2" and report["per_case"][1]["retries"] == 1
    texto = routing_evals.format_report(report)
    assert "empty_plan 25%" in texto and "acuerdo 25%" in texto and "errores {'validation': 1}" in texto


def test_cli_evals_routing_no_crea_agentes_y_escribe_json(monkeypatch, tmp_path):
    monkeypatch.setattr(minka_main, "_require_llm_backend", lambda: None)
    monkeypatch.setattr(minka_main.config, "USE_BEDROCK", False)
    monkeypatch.setattr(minka_main, "_make_orchestrator", lambda: (_ for _ in ()).throw(AssertionError("sin agentes")))
    monkeypatch.setattr(
        planner, "_llm_plan",
        lambda task, context="": planner.LLMPlan([{"agent": "architecture_agent", "task": "t"}], None, None, 0, USAGE),
    )
    out = tmp_path / "r.json"
    result = CliRunner().invoke(minka_main.app, ["evals", "routing", "--runs", "1", "--json-out", str(out)])
    assert result.exit_code == 0, result.output
    report = json.loads(out.read_text())
    assert report["runs"] == 1 and report["calls"] == report["cases"] <= 12
    assert report["empty_plan_rate"] == 0.0 and report["agreement_rate"] >= 0.9
    assert "empty_plan 0%" in result.output
