"""Sin backend LLM utilizable, minka sale con una línea que explica cómo configurarlo."""

import pytest
from typer.testing import CliRunner

from minka import main as minka_main
from minka.config_loader import config
from minka.core.agents import base_agent
from minka.core.agents.base_agent import backend_unavailable_message, build_llm_client


@pytest.fixture
def sin_backend(monkeypatch):
    monkeypatch.setattr(config, "USE_BEDROCK", False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def test_mensaje_de_una_linea_con_las_dos_opciones(sin_backend):
    msg = backend_unavailable_message()
    assert msg and "\n" not in msg
    for texto in ("LLM_BACKEND", "AWS_PROFILE", "config_local.py", "ANTHROPIC_API_KEY", ".env"):
        assert texto in msg
    with pytest.raises(RuntimeError, match="Sin backend LLM"):
        build_llm_client()


def test_cuando_hay_backend_no_hay_mensaje(sin_backend, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-...")  # placeholder de .env.example
    assert backend_unavailable_message() is not None
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-de-prueba")
    assert backend_unavailable_message() is None
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    monkeypatch.setattr(config, "USE_BEDROCK", True)
    assert backend_unavailable_message() is None


def test_cli_sale_sin_traceback(sin_backend, monkeypatch):
    monkeypatch.setattr(
        minka_main, "_make_orchestrator", lambda: pytest.fail("no debía crear el orquestador")
    )
    monkeypatch.setattr(base_agent, "_backend_warned", True)
    result = CliRunner().invoke(minka_main.app, ["analiza algo", "--no-interactive"])
    assert result.exit_code == 1
    assert "Sin backend LLM" in result.output
    assert "Traceback" not in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
