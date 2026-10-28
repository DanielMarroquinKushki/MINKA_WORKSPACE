"""Tests de resolve_model_id — idempotencia del prefijo Bedrock."""
import pytest

from minka.config_loader import config
from minka.core.agents.base_agent import resolve_model_id


@pytest.fixture
def bedrock(monkeypatch):
    monkeypatch.setattr(config, "USE_BEDROCK", True)
    monkeypatch.setattr(config, "BEDROCK_MODEL_PREFIX", "us.anthropic.")


def test_passthrough_when_not_bedrock(monkeypatch):
    monkeypatch.setattr(config, "USE_BEDROCK", False)
    assert resolve_model_id("claude-opus-4-8") == "claude-opus-4-8"


def test_bare_gets_prefix(bedrock):
    assert resolve_model_id("claude-opus-4-8") == "us.anthropic.claude-opus-4-8"


def test_anthropic_prefix_normalized(bedrock):
    assert resolve_model_id("anthropic.claude-opus-4-8") == "us.anthropic.claude-opus-4-8"


def test_idempotent_already_prefixed(bedrock):
    # ya trae el prefijo regional -> no se duplica ni se apila
    assert resolve_model_id("us.anthropic.claude-opus-4-8") == "us.anthropic.claude-opus-4-8"