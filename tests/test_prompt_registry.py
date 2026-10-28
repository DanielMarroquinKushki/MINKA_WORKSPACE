"""Tests offline del prompt versioning (fallback a archivos cuando el registro off).

La operación real (Langfuse) se valida cuando el servicio esté arriba. Aquí
bloqueamos que sin registro NADA cambia: load_system_prompt lee de archivo.
"""

from cincha.config_loader import config
from cincha.core import prompt_registry
from cincha.core.prompt_loader import load_system_prompt


def test_load_system_prompt_uses_file_when_registry_off(monkeypatch, demo_global):
    monkeypatch.setattr(config, "PROMPT_REGISTRY_ENABLED", False)
    # Deuda: core/prompts/ vacío → sin proyecto, prompt_loader.py:43 falla.
    txt = load_system_prompt("pr_review_agent")
    assert "Agente de Revisión de PRs" in txt


def test_prompt_registry_noop_when_off(monkeypatch):
    monkeypatch.setattr(config, "PROMPT_REGISTRY_ENABLED", False)
    assert prompt_registry.get_prompt("pr_review_agent") is None
    assert prompt_registry.push_prompt("x", "y") is False
