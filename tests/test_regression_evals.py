"""Tests offline del frente de evals (loader + métrica determinista).

Los runs con agente + LLM-judge NO se testean aquí (necesitan LLM/SSO); se
validan en la tanda SSO. Aquí bloqueamos lo determinista.
"""

from core.evaluator.regression_evals import (
    keyword_coverage,
    load_golden_set,
)


def test_keyword_coverage_full():
    assert keyword_coverage("menciona A y B", ["A", "B"]) == 1.0


def test_keyword_coverage_partial():
    assert keyword_coverage("solo A aquí", ["A", "B"]) == 0.5


def test_keyword_coverage_case_insensitive():
    assert keyword_coverage("DYNAMO_TOKENS y middleware", ["dynamo_tokens"]) == 1.0


def test_keyword_coverage_empty_is_one():
    assert keyword_coverage("lo que sea", []) == 1.0


def test_load_golden_set_demo(demo_project):
    cases = load_golden_set("demo", base_dir=demo_project.BASE_DIR)
    assert [c.id for c in cases] == ["demo-1"]
    assert cases[0].must_mention == ["demo"]


def test_load_golden_set_missing_returns_empty(tmp_path):
    assert load_golden_set("no-existe", base_dir=tmp_path) == []
