"""`minka init` guiado (sin sobrescribir, sin imprimir valores) y `--new-project` con
ACTIVE_CONTEXT_DIR. Sin red ni modelo."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from minka import main as minka_main
from minka.config_loader import config
from minka.core.project_scaffold import apply_init_answers, create_project, init_data_dir


@pytest.fixture
def data_dir(tmp_path, monkeypatch) -> Path:
    d = tmp_path / "home"
    monkeypatch.setattr(config, "DATA_DIR", d)
    monkeypatch.setattr(config, "PROJECTS_DIR", d / "projects", raising=False)
    monkeypatch.setattr(config, "ACTIVE_CONTEXT_DIR", None, raising=False)
    return d


def _invoke(args, **kw):
    return CliRunner().invoke(minka_main.app, args, **kw)


def test_apply_init_answers_escribe_solo_en_archivos_nuevos(data_dir):
    r = init_data_dir()
    touched = apply_init_answers(data_dir, {"pos": "/r/pos", "core": "/r/core"}, "bedrock", "mi-perfil", r["created"])
    assert len(touched) == 2
    local = (data_dir / "config_local.py").read_text(encoding="utf-8")
    assert "'pos': '/r/pos'" in local and "'core': '/r/core'" in local
    assert 'REPOS_ROOT = "~/ruta' in local  # la plantilla nueva se conserva; REPOS se agrega (esquema viejo)
    env = (data_dir / ".env").read_text(encoding="utf-8")
    assert "LLM_BACKEND=bedrock" in env and "AWS_PROFILE=mi-perfil" in env
    assert sum(1 for ln in env.splitlines() if ln.startswith("AWS_PROFILE=")) == 1

    # Segunda vez: los archivos ya existen → no se tocan.
    r2 = init_data_dir()
    assert r2["created"] == []
    assert apply_init_answers(data_dir, {"x": "/y"}, "anthropic", "", r2["created"]) == []
    assert (data_dir / "config_local.py").read_text(encoding="utf-8") == local


def test_init_guiado_pregunta_repos_y_backend_sin_imprimir_valores(data_dir):
    entrada = "\n".join(["pos", "/ruta/secreta/pos", "", "anthropic"]) + "\n"
    result = _invoke(["init"], input=entrada)
    assert result.exit_code == 0, result.output
    assert "1 repo(s) y backend anthropic" in result.output
    assert "/ruta/secreta/pos" not in result.output.replace("ruta de pos", "")
    local = (data_dir / "config_local.py").read_text(encoding="utf-8")
    assert "'pos': '/ruta/secreta/pos'" in local
    assert "LLM_BACKEND=anthropic" in (data_dir / ".env").read_text(encoding="utf-8")
    assert "ANTHROPIC_API_KEY" in result.output  # recuerda completarla, no la pide


def test_init_con_yes_solo_copia_plantillas(data_dir):
    result = _invoke(["init", "--yes"])
    assert result.exit_code == 0, result.output
    assert (data_dir / "config_local.py").is_file() and (data_dir / ".env").is_file()
    assert "creado" in result.output and "repo(s)" not in result.output


def test_init_sin_archivos_nuevos_no_pregunta(data_dir):
    init_data_dir()
    result = _invoke(["init"])
    assert result.exit_code == 0, result.output
    assert "ya existe" in result.output and "Nada que preguntar" in result.output


def test_new_project_crea_active_context_en_active_context_dir(data_dir, tmp_path, monkeypatch):
    other = tmp_path / "workspace" / "active-context"
    monkeypatch.setattr(config, "ACTIVE_CONTEXT_DIR", str(other), raising=False)
    r = create_project("mi-proj")
    assert Path(r["active_context"]) == other / "mi-proj.md"
    assert (other / "mi-proj.md").is_file()
    assert not (data_dir / "active-context" / "mi-proj.md").exists()


def test_new_project_default_sigue_en_data_dir(data_dir):
    r = create_project("otro-proj")
    assert Path(r["active_context"]) == data_dir / "active-context" / "otro-proj.md"
