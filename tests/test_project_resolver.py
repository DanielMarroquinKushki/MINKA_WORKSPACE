"""Proyecto activo por cwd (core/project_resolver.py): --project > cwd > DEFAULT_PROJECT, empates,
symlinks, configs rotos y bridge. Sin red ni modelo; proyectos y repos en tmp_path."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from minka import config_loader
from minka import main as minka_main
from minka._app import SOURCE_ENV
from minka.config_loader import config
from minka.core import project_resolver as pr

# Estos tests ejercitan la inferencia real (conftest la desactiva en el resto de la suite).
pytestmark = pytest.mark.project_inference


@pytest.fixture
def world(tmp_path, monkeypatch):
    saved = dict(config.__dict__)
    w = _build(tmp_path, monkeypatch)
    yield w
    config.__dict__.clear()
    config.__dict__.update(saved)
    pr.reset_cache()


def _build(tmp_path, monkeypatch):
    pr.reset_cache()
    clones = tmp_path / "clones"
    for name in ("svc-a", "svc-b", "svc-shared"):
        (clones / name / "service" / "deep").mkdir(parents=True)
        (clones / name / "service" / "x.go").write_text("package x\n", encoding="utf-8")
    link = tmp_path / "links"
    link.mkdir()
    (link / "svc-a").symlink_to(clones / "svc-a")
    outside = tmp_path / "otro-lugar"
    outside.mkdir()

    projects = tmp_path / "projects"
    specs = {
        "alpha": 'REPOS = {"a": "svc-a", "core": "svc-shared"}\n',
        "beta": 'REPOS = {"b": "svc-b", "core": "svc-shared"}\n',
        "roto": "REPOS = {\n",  # SyntaxError al cargar
        "_template": 'REPOS = {"x": "svc-a"}\n',  # se ignora
    }
    for name, body in specs.items():
        (projects / name / "prompts").mkdir(parents=True)
        (projects / name / "knowledge").mkdir()
        (projects / name / "config.py").write_text(f'CORE_MODULE_NAME = "svc-shared"\n{body}', encoding="utf-8")

    data = tmp_path / "data"
    data.mkdir()
    local = {"REPOS_ROOT": str(clones), "PROJECTS_DIR": str(projects)}
    monkeypatch.setattr(config_loader, "_load_local_vars", lambda data_dir: dict(local))
    monkeypatch.setattr(config_loader._base, "PROJECTS_DIR", projects)
    monkeypatch.setattr(config_loader._base, "DATA_DIR", data)
    monkeypatch.setattr(config, "PROJECTS_DIR", projects, raising=False)
    monkeypatch.setattr(config, "DATA_DIR", data, raising=False)
    monkeypatch.setattr(config, "ACTIVE_PROJECT", None, raising=False)
    monkeypatch.delenv(SOURCE_ENV, raising=False)
    return dict(clones=clones, link=link, outside=outside, projects=projects, local=local)


def _decide(world, cwd, explicit=None, default=None, bridge=False):
    local = dict(world["local"])
    if default:
        local["DEFAULT_PROJECT"] = default
    return pr.decide(explicit, Path(cwd), projects_dir=world["projects"], local_values=local, bridge=bridge)


# ---------------------------------------------------------------------------
# decide()
# ---------------------------------------------------------------------------


def test_cwd_en_repo_y_subcarpeta(world):
    d = _decide(world, world["clones"] / "svc-a")
    assert (d.name, d.origin) == ("alpha", pr.ORIGIN_CWD)
    d = _decide(world, world["clones"] / "svc-b" / "service" / "deep")
    assert (d.name, d.origin) == ("beta", pr.ORIGIN_CWD)
    assert "roto" in " ".join(d.notices) and "omitido" in " ".join(d.notices)


def test_cwd_via_symlink(world):
    d = _decide(world, world["link"] / "svc-a" / "service")
    assert (d.name, d.origin) == ("alpha", pr.ORIGIN_CWD)


def test_fuera_de_todos_usa_default_o_falla(world):
    d = _decide(world, world["outside"], default="beta")
    assert (d.name, d.origin) == ("beta", pr.ORIGIN_DEFAULT)
    d = _decide(world, world["outside"])
    assert d.name is None and d.error and "--project" in d.error and "--new-project" in d.error


def test_explicito_gana_siempre(world):
    d = _decide(world, world["clones"] / "svc-a", explicit="beta", default="alpha")
    assert (d.name, d.origin) == ("beta", pr.ORIGIN_FLAG)


def test_empate_con_y_sin_default(world):
    shared = world["clones"] / "svc-shared" / "service"
    d = _decide(world, shared, default="beta")
    assert (d.name, d.origin) == ("beta", pr.ORIGIN_DEFAULT) and sorted(d.candidates) == ["alpha", "beta"]
    d = _decide(world, shared, default="gamma")  # default que no es candidato: no decide solo
    assert d.name is None and d.ambiguous and sorted(d.candidates) == ["alpha", "beta"]
    d = _decide(world, shared)
    assert d.ambiguous


def test_bridge_exige_project(world):
    d = _decide(world, world["clones"] / "svc-a", bridge=True)
    assert d.name is None and "bridge" in d.error and "--project" in d.error
    d = _decide(world, world["clones"] / "svc-a", explicit="alpha", bridge=True)
    assert d.name == "alpha"


def test_cache_por_proceso(world, monkeypatch):
    cwd = world["clones"] / "svc-a"
    _decide(world, cwd)
    calls = []
    monkeypatch.setattr(config_loader, "_load_project_vars", lambda *a, **k: calls.append(1) or {})
    _decide(world, cwd)
    assert calls == []  # misma cwd: no vuelve a leer los configs
    pr.reset_cache()
    _decide(world, cwd)
    assert calls  # tras limpiar la caché sí relee


def test_plantilla_no_cuenta(world):
    # _template también "contiene" svc-a pero nunca es candidato.
    d = _decide(world, world["clones"] / "svc-a")
    assert d.name == "alpha" and "_template" not in d.candidates


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class _FakeOrch:
    def __init__(self):
        self.memory = []
        self.last_run_record = {}
        self.last_run_ok = True

    def run(self, task, **kwargs):
        return "ok"


@pytest.fixture
def cli(world, monkeypatch):
    fake = _FakeOrch()
    monkeypatch.setattr(minka_main, "_require_llm_backend", lambda: None)
    monkeypatch.setattr(minka_main, "_make_orchestrator", lambda: fake)
    monkeypatch.setattr(config, "OUTPUT_DIR", world["projects"].parent / "out", raising=False)
    return fake


def _run(args, **kw):
    return CliRunner().invoke(minka_main.app, args, **kw)


def test_cli_infiere_por_cwd_y_lo_anuncia(world, cli, monkeypatch):
    monkeypatch.chdir(world["clones"] / "svc-b" / "service")
    result = _run(["hola"])
    assert result.exit_code == 0, result.output
    assert "Proyecto activo: beta (por cwd)" in result.output
    assert config.ACTIVE_PROJECT == "beta"


def test_cli_project_explicito(world, cli, monkeypatch):
    monkeypatch.chdir(world["clones"] / "svc-b")
    result = _run(["hola", "--project", "alpha"])
    assert result.exit_code == 0 and "Proyecto activo: alpha (por --project)" in result.output


def test_cli_empate_sin_tty_falla_con_lista(world, cli, monkeypatch):
    monkeypatch.chdir(world["clones"] / "svc-shared")
    result = _run(["hola"])  # CliRunner no es TTY
    assert result.exit_code == 2
    assert "alpha" in result.output and "beta" in result.output and "--project" in result.output


def test_cli_empate_con_default_lo_resuelve(world, cli, monkeypatch):
    world["local"]["DEFAULT_PROJECT"] = "alpha"
    monkeypatch.chdir(world["clones"] / "svc-shared")
    result = _run(["hola"])
    assert result.exit_code == 0 and "Proyecto activo: alpha (por DEFAULT_PROJECT)" in result.output


def test_cli_empate_con_tty_pregunta(world, cli, monkeypatch):
    monkeypatch.chdir(world["clones"] / "svc-shared")
    monkeypatch.setattr(minka_main, "_is_tty", lambda: True)  # CliRunner no es terminal
    result = _run(["hola"], input="2\n")
    assert result.exit_code == 0, result.output
    assert "Proyecto activo: beta (por elección)" in result.output


def test_cli_fuera_de_todo_sin_default(world, cli, monkeypatch):
    monkeypatch.chdir(world["outside"])
    result = _run(["hola"])
    assert result.exit_code == 2 and "--new-project" in result.output


def test_cli_bridge_sin_project(world, cli, monkeypatch):
    monkeypatch.chdir(world["clones"] / "svc-a")
    monkeypatch.setenv(SOURCE_ENV, "bridge")
    result = _run(["hola"])
    assert result.exit_code == 2 and "bridge" in result.output and "--project" in result.output
    result = _run(["hola", "--project", "alpha"])
    assert result.exit_code == 0


def test_cli_config_roto_se_avisa_solo_con_debug(world, cli, monkeypatch):
    monkeypatch.chdir(world["clones"] / "svc-a")
    plain = _run(["hola"])
    assert plain.exit_code == 0 and "omitido" not in plain.output
    pr.reset_cache()
    with_debug = _run(["hola", "--debug"])
    assert with_debug.exit_code == 0 and "roto" in with_debug.output and "omitido" in with_debug.output


def test_doctor_muestra_el_origen(world, monkeypatch):
    monkeypatch.chdir(world["clones"] / "svc-b")
    monkeypatch.setattr(config, "USE_BEDROCK", False, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    result = _run(["doctor"])
    assert "beta (por cwd)" in result.output
    result = _run(["doctor", "--project", "alpha"])
    assert "alpha (por --project)" in result.output
