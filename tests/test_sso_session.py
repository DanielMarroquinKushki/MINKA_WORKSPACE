"""ensure_sso_login y resolución del perfil AWS, sin red, sin login real y sin tocar ~/.aws."""

import json
import subprocess
from datetime import datetime, timedelta, timezone

import pytest
from botocore.exceptions import EndpointConnectionError, UnauthorizedSSOTokenError

from minka import aws_session_manager as asm
from minka import config as config_py
from minka import config_loader
from minka.config_loader import config
from minka.core.agents import base_agent

PERFIL = "perfil-test"


@pytest.fixture
def mgr(monkeypatch, tmp_path):
    m = asm.AWSSessionManager(
        profile_name=PERFIL, region="region-test", login_timeout_seconds=7
    )
    monkeypatch.setattr(asm, "_SSO_CACHE_DIR", tmp_path / "sin-cache")  # nunca ~/.aws real
    monkeypatch.setattr("time.sleep", lambda *_: pytest.fail("no debe haber esperas"))
    return m


def _token(mgr, monkeypatch, valid: bool, minutes: int | None = 60):
    monkeypatch.setattr(
        mgr, "sso_token_status", lambda: {"valid": valid, "minutes_remaining": minutes}
    )


def _sts(mgr, monkeypatch, *effects):
    """Cada llamada a _sts_check consume un efecto: None = OK, excepción = falla."""
    calls = list(effects)

    def fake():
        effect = calls.pop(0)
        if effect is not None:
            raise effect

    monkeypatch.setattr(mgr, "_sts_check", fake)
    return calls


def _subprocess(monkeypatch, effect=None):
    seen = []

    def fake(cmd, **kwargs):
        seen.append((cmd, kwargs))
        if effect is not None:
            raise effect

    monkeypatch.setattr(asm.subprocess, "run", fake)
    return seen


def _no_sts(mgr, monkeypatch):
    monkeypatch.setattr(mgr, "_sts_check", lambda: pytest.fail("no debía llamar a STS"))


TOKEN_ERR = UnauthorizedSSOTokenError()
NET_ERR = EndpointConnectionError(endpoint_url="https://sts.invalid")


def test_token_vigente_no_toca_la_red(mgr, monkeypatch):
    _token(mgr, monkeypatch, valid=True, minutes=60)
    _no_sts(mgr, monkeypatch)
    seen = _subprocess(monkeypatch)
    assert mgr.ensure_sso_login() == (True, "")
    assert seen == []


def test_vencido_con_renovacion_silenciosa_no_hace_login(mgr, monkeypatch):
    _token(mgr, monkeypatch, valid=False, minutes=None)
    _sts(mgr, monkeypatch, None)
    seen = _subprocess(monkeypatch)
    assert mgr.ensure_sso_login() == (True, "")
    assert seen == []


def test_vencido_con_tty_lanza_login_sin_capturar_salida(mgr, monkeypatch):
    _token(mgr, monkeypatch, valid=False, minutes=None)
    _sts(mgr, monkeypatch, TOKEN_ERR, None)
    monkeypatch.setattr(asm, "_is_interactive", lambda: True)
    seen = _subprocess(monkeypatch)
    ok, msg = mgr.ensure_sso_login()
    assert ok and "renovada" in msg
    assert seen == [(["aws", "sso", "login", "--profile", PERFIL], {"check": True, "timeout": 7})]


def test_vencido_sin_tty_error_inmediato(mgr, monkeypatch):
    _token(mgr, monkeypatch, valid=False, minutes=None)
    _sts(mgr, monkeypatch, TOKEN_ERR)
    monkeypatch.setattr(asm, "_is_interactive", lambda: False)
    seen = _subprocess(monkeypatch)
    ok, msg = mgr.ensure_sso_login()
    assert not ok
    assert f"aws sso login --profile {PERFIL}" in msg
    assert seen == []


def test_red_caida_no_lanza_login(mgr, monkeypatch):
    _token(mgr, monkeypatch, valid=False, minutes=None)
    _sts(mgr, monkeypatch, NET_ERR)
    monkeypatch.setattr(asm, "_is_interactive", lambda: True)
    seen = _subprocess(monkeypatch)
    ok, msg = mgr.ensure_sso_login()
    assert not ok and "Sin conexión" in msg
    assert seen == []


@pytest.mark.parametrize(
    "effect, texto",
    [
        (subprocess.TimeoutExpired(cmd="aws", timeout=7), "no terminó en 7 s"),
        (FileNotFoundError(), "No se encontró la CLI 'aws'"),
    ],
)
def test_login_timeout_o_sin_cli(mgr, monkeypatch, effect, texto):
    _token(mgr, monkeypatch, valid=False, minutes=None)
    _sts(mgr, monkeypatch, TOKEN_ERR)
    monkeypatch.setattr(asm, "_is_interactive", lambda: True)
    _subprocess(monkeypatch, effect)
    ok, msg = mgr.ensure_sso_login()
    assert not ok and texto in msg


def test_token_de_otro_start_url_no_cuenta(mgr, monkeypatch, tmp_path):
    exp = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    (tmp_path / "otro.json").write_text(
        json.dumps({"accessToken": "x", "expiresAt": exp, "startUrl": "https://otro"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(asm, "_SSO_CACHE_DIR", tmp_path)
    assert mgr._cached_token_expiry("https://perfil") == (None, None)

    (tmp_path / "mio.json").write_text(
        json.dumps({"accessToken": "x", "expiresAt": exp, "startUrl": "https://perfil"}),
        encoding="utf-8",
    )
    _, minutes = mgr._cached_token_expiry("https://perfil")
    assert minutes is not None and minutes > 0


def test_precedencia_del_perfil(tmp_path, monkeypatch):
    monkeypatch.setattr(config_py, "AWS_PROFILE", "del-entorno")  # lo que leyó config.py
    monkeypatch.setattr(config_loader._base, "DATA_DIR", tmp_path)
    assert config_loader.load_config().AWS_PROFILE == "del-entorno"

    (tmp_path / "config_local.py").write_text('AWS_PROFILE = "local"\n', encoding="utf-8")
    assert config_loader.load_config().AWS_PROFILE == "local"

    # get_aws_session y AnthropicBedrock usan el valor efectivo del namespace.
    monkeypatch.setattr(config, "AWS_PROFILE", "local")
    monkeypatch.setattr(config_py, "_aws_session", None)
    assert config.get_aws_session().profile_name == "local"

    captured = {}
    monkeypatch.setattr(
        "anthropic.AnthropicBedrock", lambda **kw: captured.update(kw) or object()
    )
    asm.build_anthropic_bedrock(config)
    assert captured["aws_profile"] == "local"
    assert captured["timeout"].read == config.BEDROCK_TIMEOUT_SECONDS
    assert captured["timeout"].connect == config.BEDROCK_CONNECT_TIMEOUT_SECONDS
    assert captured["max_retries"] == config.BEDROCK_MAX_RETRIES


def test_backend_inferido_avisa(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config_loader._base, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config_loader._base, "LLM_BACKEND", "")
    monkeypatch.setenv("AWS_PROFILE", "algun-perfil")
    cfg = config_loader.load_config()
    assert cfg.USE_BEDROCK and cfg.BACKEND_INFERRED

    monkeypatch.setattr(config, "BACKEND_INFERRED", True)
    monkeypatch.setattr(base_agent, "_backend_warned", False)
    base_agent._warn_if_backend_inferred()
    base_agent._warn_if_backend_inferred()
    err = capsys.readouterr().err
    assert err.count("LLM_BACKEND no está definido") == 1


def _gate_vencido_sin_tty(mgr, monkeypatch):
    """Sesión del config global = mgr con token vencido, sin TTY y sin login posible."""
    _token(mgr, monkeypatch, valid=False, minutes=None)
    _sts(mgr, monkeypatch, TOKEN_ERR)
    monkeypatch.setattr(asm, "_is_interactive", lambda: False)
    monkeypatch.setattr(config, "USE_BEDROCK", True)
    monkeypatch.setattr(config, "get_aws_session", lambda: mgr)
    return _subprocess(monkeypatch)


def test_regression_evals_aborta_sin_tty(mgr, monkeypatch, demo_global):
    from minka.core.evaluator.regression_evals import run_evaluation

    seen = _gate_vencido_sin_tty(mgr, monkeypatch)
    monkeypatch.setattr(config, "EVALUATOR_ENABLED", True)
    r = run_evaluation("demo", use_geval=False)
    assert f"aws sso login --profile {PERFIL}" in r["error"]
    assert seen == []


def test_prompt_optimizer_aborta_sin_tty(mgr, monkeypatch, demo_global):
    from minka.core.evaluator.prompt_optimizer import PromptOptimizer

    seen = _gate_vencido_sin_tty(mgr, monkeypatch)
    monkeypatch.setattr(config, "META_AGENT_ENABLED", True)
    monkeypatch.setattr(config, "PROMPT_OPTIMIZATION_ENABLED", True)
    monkeypatch.setattr(
        PromptOptimizer, "_collect_feedback", lambda *a: pytest.fail("no debía correr evals")
    )
    r = PromptOptimizer(config).propose("pr_review_agent", "demo")
    assert f"aws sso login --profile {PERFIL}" in r["error"]
    assert seen == []
