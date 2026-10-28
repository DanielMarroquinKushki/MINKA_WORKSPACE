"""Tests de create_project — aislados en un workspace temporal (no toca el árbol real)."""
import shutil
from pathlib import Path

import pytest

import config_loader
from config_loader import config
from core.project_scaffold import (
    InvalidProjectNameError,
    ProjectExistsError,
    create_project,
)


@pytest.fixture
def fake_workspace(tmp_path, monkeypatch):
    """Copia el _template real a un workspace temporal y apunta config.BASE_DIR ahí."""
    real_base = Path(config.BASE_DIR)
    fake_base = tmp_path / "dev-agent"
    (fake_base / "projects").mkdir(parents=True)
    shutil.copytree(
        real_base / "projects" / "_template", fake_base / "projects" / "_template"
    )
    monkeypatch.setattr(config, "BASE_DIR", fake_base)
    return tmp_path


def test_create_project_builds_structure(fake_workspace):
    r = create_project("mi-proj", core_module="usrv-x", workspace_root=fake_workspace)
    pd = Path(r["project_dir"])
    assert (pd / "config.py").is_file()
    assert not (pd / "config.py.example").exists()
    assert (pd / "knowledge/team/session_log.md").is_file()
    assert (pd / "knowledge/team/decisions_log.md").is_file()
    assert Path(r["active_context"]).is_file()
    assert 'CORE_MODULE_NAME = "usrv-x"' in (pd / "config.py").read_text()


def test_create_project_rejects_duplicate(fake_workspace):
    create_project("dup", workspace_root=fake_workspace)
    with pytest.raises(ProjectExistsError):
        create_project("dup", workspace_root=fake_workspace)


def test_create_project_rejects_template(fake_workspace):
    with pytest.raises(InvalidProjectNameError):
        create_project("_template", workspace_root=fake_workspace)


def test_create_project_rejects_invalid_name(fake_workspace):
    with pytest.raises(InvalidProjectNameError):
        create_project("Mal Nombre", workspace_root=fake_workspace)


def test_proyecto_nuevo_carga_sin_team_pillars(fake_workspace, monkeypatch):
    create_project("mi-proj", workspace_root=fake_workspace)
    projects = Path(config.BASE_DIR) / "projects"
    monkeypatch.setattr(config_loader, "_PROJECTS_DIR", projects)
    cfg = config_loader.load_config("mi-proj")
    assert cfg.TEAM_PILLARS == []
    assert "team/team_pillars.md" in cfg.AGENT_KNOWLEDGE["development_agent"]
    assert (projects / "mi-proj" / "knowledge" / "team" / "team_pillars.md").is_file()
