"""Tests offline del prompt_optimizer (lógica pura + gate).

El ciclo real (correr agentes + juez + meta-LLM) se valida en vivo con SSO.
Aquí bloqueamos lo determinista y el gate de nivel.
"""
import pytest

from cincha.config_loader import config
from cincha.core.evaluator.prompt_optimizer import PromptOptimizer, _missed_keywords


def test_missed_keywords():
    assert _missed_keywords("contiene ALPHA solamente", ["ALPHA", "BETA", "GAMMA"]) == [
        "BETA",
        "GAMMA",
    ]


def test_missed_keywords_case_insensitive():
    assert _missed_keywords("usa DYNAMO_TOKENS aquí", ["dynamo_tokens"]) == []


def test_missed_keywords_empty():
    assert _missed_keywords("lo que sea", []) == []


def test_optimizer_gated_when_disabled(monkeypatch):
    monkeypatch.setattr(config, "META_AGENT_ENABLED", False)
    with pytest.raises(NotImplementedError):
        PromptOptimizer(config)