"""Tests offline de four_r_review — agregación pura + fallback (sin LLM)."""

from cincha.config_loader import config
from cincha.core.orchestrator.four_r_review import (
    DimensionFinding,
    FourRReviewResult,
    _lens_finding,
    run_four_r_review,
)


def test_no_blockers_routes_aprobar():
    res = FourRReviewResult(
        findings=[
            DimensionFinding("Riesgo", "CUMPLE", "", ""),
            DimensionFinding("Legibilidad", "REVISAR", "", "renombrar x"),
        ]
    )
    assert res.has_blockers is False
    assert res.routing_decision == "aprobar"
    assert res.blockers == []


def test_blocker_routes_corregir():
    res = FourRReviewResult(
        findings=[DimensionFinding("Riesgo", "BLOQUEANTE", "keys.go:12", "usar SSM")]
    )
    assert res.has_blockers is True
    assert res.routing_decision == "corregir"
    assert res.blockers == ["[Riesgo] usar SSM"]


def test_summary_contains_dimension_and_status():
    res = FourRReviewResult(findings=[DimensionFinding("Robustez", "CUMPLE", "", "")])
    assert "Robustez" in res.summary
    assert "CUMPLE" in res.summary


def test_lens_finding_fallback_when_no_structured(monkeypatch):
    from cincha.core import structured as structured

    monkeypatch.setattr(structured, "call_structured", lambda *a, **k: None)
    f = _lens_finding("Riesgo", "prompt", "diff", client=None, model="m")
    assert f.status == "REVISAR"


def test_run_four_r_disabled_returns_none(monkeypatch):
    monkeypatch.setattr(config, "EVALUATOR_ENABLED", False)
    assert run_four_r_review("diff", client=None) is None
