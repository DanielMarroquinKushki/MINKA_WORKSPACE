"""Tests del routing por keywords del planner (determinista, sin LLM)."""
from minka.core.orchestrator.planner import _keyword_plan, make_plan


def test_keyword_development():
    assert _keyword_plan("corrige el bug")[0]["agent"] == "development_agent"


def test_keyword_pr_review():
    assert _keyword_plan("revisa pr del cambio")[0]["agent"] == "pr_review_agent"


def test_keyword_debug():
    assert _keyword_plan("hay un panic en el flujo")[0]["agent"] == "debug_agent"


def test_keyword_report():
    assert _keyword_plan("genera reporte de la sesión")[0]["agent"] == "report_agent"


def test_keyword_default_architecture():
    # sin keywords reconocidas -> default architecture (solo lectura, lo más seguro)
    assert _keyword_plan("holaaa")[0]["agent"] == "architecture_agent"


def test_make_plan_uses_keyword_when_no_llm():
    plan = make_plan("corrige el bug", use_llm=False)
    assert plan == [{"agent": "development_agent", "task": "corrige el bug"}]