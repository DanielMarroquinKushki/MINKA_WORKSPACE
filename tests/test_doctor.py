"""`minka doctor` (core/doctor.py): chequeos locales con ✓/✗ y qué hacer, sin red."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from minka import main as minka_main
from minka.config_loader import config
from minka.core import doctor

NOW = datetime(2026, 1, 10, 12, 0, tzinfo=timezone.utc)


def _sso_cache(tmp_path: Path, delta: timedelta | None) -> Path:
    cache = tmp_path / "sso-cache"
    cache.mkdir(parents=True, exist_ok=True)
    if delta is not None:
        (cache / "tok.json").write_text(
            json.dumps({"accessToken": "x", "expiresAt": (NOW + delta).strftime("%Y-%m-%dT%H:%M:%SZ")}),
            encoding="utf-8",
        )
    (cache / "otro.json").write_text(json.dumps({"clientId": "c"}), encoding="utf-8")
    (cache / "roto.json").write_text("{no json", encoding="utf-8")
    return cache


def _cfg(tmp_path: Path, **over) -> SimpleNamespace:
    data = tmp_path / "data"
    (data / "projects" / "demo").mkdir(parents=True, exist_ok=True)
    (data / "active-context").mkdir(exist_ok=True)
    (data / "active-context" / "demo.md").write_text("- ticket", encoding="utf-8")
    (data / "config_local.py").write_text("", encoding="utf-8")
    (data / ".env").write_text("", encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    base = dict(
        DATA_DIR=data, PROJECTS_DIR=data / "projects", ACTIVE_CONTEXT_DIR=None, ACTIVE_PROJECT="demo",
        USE_BEDROCK=True, AWS_PROFILE="perfil", REPOS={"pos": str(repo)}, RAG_ENABLED=False,
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_sso_token_minutes_lee_solo_cache_local(tmp_path):
    assert doctor.sso_token_minutes(_sso_cache(tmp_path, timedelta(minutes=90)), NOW) == 90
    assert doctor.sso_token_minutes(_sso_cache(tmp_path / "b", timedelta(minutes=-5)), NOW) == -5
    assert doctor.sso_token_minutes(_sso_cache(tmp_path / "c", None), NOW) is None
    assert doctor.sso_token_minutes(tmp_path / "no-existe", NOW) is None


def test_todo_en_orden(tmp_path):
    checks = doctor.run_checks(_cfg(tmp_path), sso_cache_dir=_sso_cache(tmp_path, timedelta(hours=1)), now=NOW)
    by = {c.name: c for c in checks}
    for name in ("Python", "Directorio de datos", "Sesión AWS", "Repos", "chromadb", "Proyecto activo", "Contexto vivo"):
        assert by[name].ok, name
    assert not doctor.has_critical_failure(checks)
    report = doctor.format_report(checks)
    assert "✗" not in report and "Todo en orden" in report


def test_fallos_con_hint_y_bloqueantes(tmp_path):
    cfg = _cfg(tmp_path, REPOS={"pos": str(tmp_path / "no"), "hsm": ""}, ACTIVE_PROJECT="otro")
    checks = doctor.run_checks(cfg, sso_cache_dir=_sso_cache(tmp_path, timedelta(minutes=-1)), now=NOW)
    by = {c.name: c for c in checks}
    assert not by["Sesión AWS"].ok and "aws sso login --profile perfil" in by["Sesión AWS"].hint
    assert not by["Repos"].ok and "faltan: pos" in by["Repos"].detail
    assert str(tmp_path) not in by["Repos"].detail  # nunca rutas en la salida
    assert not by["Proyecto activo"].ok and "otro" in by["Proyecto activo"].detail
    assert not by["Contexto vivo"].ok and not by["Contexto vivo"].critical
    assert doctor.has_critical_failure(checks)
    report = doctor.format_report(checks)
    assert "✗ Repos" in report and "→ Corrige las rutas" in report and "bloqueantes" in report


def test_env_ausente_avisa_sin_bloquear_y_config_local_ausente_bloquea(tmp_path):
    cfg = _cfg(tmp_path)
    (cfg.DATA_DIR / ".env").unlink()
    by = {c.name: c for c in doctor.run_checks(cfg, sso_cache_dir=tmp_path / "x", now=NOW)}
    assert not by["Directorio de datos"].ok and not by["Directorio de datos"].critical
    assert by["Directorio de datos"].detail == "falta .env"
    (cfg.DATA_DIR / "config_local.py").unlink()
    by = {c.name: c for c in doctor.run_checks(cfg, sso_cache_dir=tmp_path / "x", now=NOW)}
    assert by["Directorio de datos"].critical and "config_local.py" in by["Directorio de datos"].detail


def test_env_ausente_es_opcional_si_config_local_define_backend(tmp_path):
    cfg = _cfg(tmp_path)
    (cfg.DATA_DIR / ".env").unlink()
    (cfg.DATA_DIR / "config_local.py").write_text('LLM_BACKEND = "bedrock"\n', encoding="utf-8")
    checks = doctor.run_checks(cfg, sso_cache_dir=_sso_cache(tmp_path, timedelta(hours=1)), now=NOW)
    by = {c.name: c for c in checks}
    c = by["Directorio de datos"]
    assert c.ok and c.optional and c.mark == "–" and "opcional" in c.detail
    assert not doctor.has_critical_failure(checks)
    report = doctor.format_report(checks)
    assert "– Directorio de datos" in report and "Todo en orden" in report
    # Solo AWS_PROFILE también cuenta como backend definido.
    (cfg.DATA_DIR / "config_local.py").write_text('AWS_PROFILE = "perfil"\n', encoding="utf-8")
    by = {c.name: c for c in doctor.run_checks(cfg, sso_cache_dir=tmp_path / "x", now=NOW)}
    assert by["Directorio de datos"].optional


def test_api_directa_pide_api_key(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = _cfg(tmp_path, USE_BEDROCK=False)
    by = {c.name: c for c in doctor.run_checks(cfg, now=NOW)}
    assert "Sesión AWS" not in by and not by["API key"].ok
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    by = {c.name: c for c in doctor.run_checks(cfg, now=NOW)}
    assert by["API key"].ok and "sk-test" not in by["API key"].detail


def test_chromadb_solo_bloquea_con_rag(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "_module_available", lambda name: False)
    by = {c.name: c for c in doctor.run_checks(_cfg(tmp_path), sso_cache_dir=tmp_path / "x", now=NOW)}
    assert by["chromadb"].ok and not by["chromadb"].critical  # RAG apagado: solo informa
    by = {c.name: c for c in doctor.run_checks(_cfg(tmp_path, RAG_ENABLED=True), sso_cache_dir=tmp_path / "x", now=NOW)}
    assert not by["chromadb"].ok and by["chromadb"].critical


def test_cli_doctor_exit_code(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    for key, value in vars(cfg).items():
        monkeypatch.setattr(config, key, value, raising=False)
    monkeypatch.setattr(doctor, "sso_token_minutes", lambda *a, **k: 30)
    result = CliRunner().invoke(minka_main.app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "✓ Repos" in result.output
    monkeypatch.setattr(config, "REPOS", {}, raising=False)
    result = CliRunner().invoke(minka_main.app, ["doctor"])
    assert result.exit_code == 1 and "✗ Repos" in result.output
