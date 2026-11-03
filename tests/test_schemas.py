"""Tests de los modelos Pydantic de salidas estructuradas."""
import pytest
from pydantic import ValidationError

from minka.core.schemas import Plan, ReviewDecision


def test_plan_valid():
    p = Plan.model_validate({"steps": [{"agent": "development_agent", "task": "corrige X"}]})
    assert p.steps[0].agent == "development_agent"
    assert p.steps[0].task == "corrige X"


def test_plan_rejects_unknown_agent():
    with pytest.raises(ValidationError):
        Plan.model_validate({"steps": [{"agent": "inventado", "task": "x"}]})


def test_plan_requires_at_least_one_step():
    # steps=[] ya no valida (minItems=1): el planner cae a la red de seguridad del router.
    with pytest.raises(ValidationError):
        Plan.model_validate({"steps": []})
    with pytest.raises(ValidationError):
        Plan()


def test_review_decision_valid_with_blockers():
    rd = ReviewDecision.model_validate({"routing_decision": "corregir", "blockers": ["R-02"]})
    assert rd.routing_decision == "corregir"
    assert rd.blockers == ["R-02"]


def test_review_decision_default_blockers():
    assert ReviewDecision.model_validate({"routing_decision": "aprobar"}).blockers == []


def test_review_decision_rejects_invalid_routing():
    with pytest.raises(ValidationError):
        ReviewDecision.model_validate({"routing_decision": "quiza"})