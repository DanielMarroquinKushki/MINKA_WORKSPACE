"""Tests de create_project — aislados en un workspace temporal (no toca el árbol real)."""
from pathlib import Path

import pytest

from cincha import config_loader
from cincha.config_loader import config
from cincha.core.project_scaffold import (
    InvalidProjectNameError,
    ProjectExistsError,
    create_project,
)


@pytest.fixture
def fake_workspace(tmp_path, monkeypatch):
    """Apunta DATA_DIR y PROJECTS_DIR a tmp_path (la plantilla sale de TEMPLATE_DIR)."""
    fake_projects = tmp_path / "projects"
    fake_projects.mkdir(parents=True)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "PROJECTS_DIR", fake_projects)
    return tmp_path


def test_create_project_builds_structure(fake_workspace):
    r = create_project("mi-proj", core_module="usrv-x")
    pd = Path(r["project_dir"])
    assert (pd / "config.py").is_file()
    assert not (pd / "config.py.example").exists()
    assert (pd / "knowledge/team/session_log.md").is_file()
    assert (pd / "knowledge/team/decisions_log.md").is_file()
    assert Path(r["active_context"]) == fake_workspace / "active-context" / "mi-proj.md"
    assert Path(r["active_context"]).is_file()
    assert not (Path(config.BASE_DIR).parent / "active-context" / "mi-proj.md").exists()
    assert 'CORE_MODULE_NAME = "usrv-x"' in (pd / "config.py").read_text()


def test_create_project_rejects_duplicate(fake_workspace):
    create_project("dup")
    with pytest.raises(ProjectExistsError):
        create_project("dup")


def test_create_project_rejects_template(fake_workspace):
    with pytest.raises(InvalidProjectNameError):
        create_project("_template")


def test_create_project_rejects_invalid_name(fake_workspace):
    with pytest.raises(InvalidProjectNameError):
        create_project("Mal Nombre")


def test_proyecto_nuevo_carga_sin_team_pillars(fake_workspace, monkeypatch):
    create_project("mi-proj")
    projects = Path(config.PROJECTS_DIR)
    cfg = config_loader.load_config("mi-proj", projects_dir=projects)
    assert cfg.TEAM_PILLARS == []
    assert "team/team_pillars.md" in cfg.AGENT_KNOWLEDGE["development_agent"]
    assert (projects / "mi-proj" / "knowledge" / "team" / "team_pillars.md").is_file()
