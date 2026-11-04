"""REPOS por proyecto (config_loader.resolve_repos): los alias viven en projects/<p>/config.py,
config_local aporta REPOS_ROOT y REPOS_OVERRIDES; el esquema viejo sigue funcionando.
Dos proyectos con repos distintos no se mezclan en list_repos, citation_check ni knowledge check."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from minka import config_loader
from minka import main as minka_main
from minka.config_loader import config, resolve_repos
from minka.core import doctor
from minka.core.orchestrator import citation_check
from minka.core.tools import registry
from minka.core.tools.analysis import citations as cit


@pytest.fixture
def world(tmp_path: Path):
    """Dos proyectos (alpha, beta) con repos distintos bajo una raíz común, más un repo suelto."""
    root = tmp_path / "clones"
    for name, files in (
        ("svc-a", {"service/a.go": "package a\n" * 10}),
        ("svc-b", {"service/b.go": "package b\n" * 10}),
        ("svc-shared", {"core/x.go": "package x\n"}),
    ):
        for rel, text in files.items():
            p = root / name / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
    elsewhere = tmp_path / "elsewhere" / "svc-b-local"
    (elsewhere / "service").mkdir(parents=True)
    (elsewhere / "service" / "b.go").write_text("package b\n" * 50, encoding="utf-8")

    projects = tmp_path / "projects"
    for name, repos in (
        ("alpha", '{"a": "svc-a", "core": "svc-shared"}'),
        ("beta", '{"b": "svc-b", "core": "svc-shared"}'),
        ("legacy", None),
    ):
        pdir = projects / name
        (pdir / "knowledge" / "extracted").mkdir(parents=True)
        (pdir / "prompts").mkdir()
        body = 'CORE_MODULE_NAME = "svc-shared"\n' + (f"REPOS = {repos}\n" if repos else "")
        (pdir / "config.py").write_text(body, encoding="utf-8")
    (projects / "alpha" / "knowledge" / "extracted" / "architecture.md").write_text(
        "Ver service/a.go:3 y service/b.go:3\n", encoding="utf-8"
    )
    (projects / "beta" / "knowledge" / "extracted" / "architecture.md").write_text(
        "Ver service/b.go:3 y service/a.go:3\n", encoding="utf-8"
    )
    return dict(root=root, projects=projects, elsewhere=elsewhere)


def _local(monkeypatch, **values):
    monkeypatch.setattr(config_loader, "_load_local_vars", lambda data_dir: dict(values))


# ---------------------------------------------------------------------------
# resolve_repos (unidad)
# ---------------------------------------------------------------------------


def test_resolve_relativas_con_root_y_overrides(world):
    repos, source = resolve_repos(
        {"REPOS": {"a": "svc-a", "abs": "/abs/x"}},
        {"REPOS_ROOT": str(world["root"]), "REPOS_OVERRIDES": {"a": str(world["elsewhere"])}},
    )
    assert source == "project"
    assert repos["a"] == str(world["elsewhere"])  # override local gana
    assert repos["abs"] == "/abs/x"  # absoluta se respeta


def test_root_del_proyecto_y_tilde(world, monkeypatch):
    monkeypatch.setenv("HOME", str(world["root"].parent))
    repos, _ = resolve_repos({"REPOS": {"a": "svc-a"}, "REPOS_ROOT": "~/clones"}, {})
    assert repos["a"] == str(world["root"] / "svc-a")
    # REPOS_ROOT local gana sobre el del proyecto.
    repos, _ = resolve_repos({"REPOS": {"a": "svc-a"}, "REPOS_ROOT": "/no"}, {"REPOS_ROOT": str(world["root"])})
    assert repos["a"] == str(world["root"] / "svc-a")


def test_esquema_viejo_y_vacio(world):
    legacy = {"REPOS": {"a": str(world["root"] / "svc-a")}}
    assert resolve_repos({}, legacy) == ({"a": str(world["root"] / "svc-a")}, "local")
    # El proyecto gana sobre el REPOS viejo de config_local.
    repos, source = resolve_repos({"REPOS": {"b": "svc-b"}}, {**legacy, "REPOS_ROOT": str(world["root"])})
    assert source == "project" and list(repos) == ["b"]
    assert resolve_repos({}, {}) == ({}, "none")
    assert resolve_repos({"REPOS": {"a": ""}}, {})[0] == {"a": ""}


# ---------------------------------------------------------------------------
# load_config: A y B no se mezclan
# ---------------------------------------------------------------------------


def test_load_config_por_proyecto_no_mezcla(world, monkeypatch):
    _local(monkeypatch, REPOS_ROOT=str(world["root"]))
    a = config_loader.load_config("alpha", projects_dir=world["projects"])
    b = config_loader.load_config("beta", projects_dir=world["projects"])
    assert set(a.REPOS) == {"a", "core"} and set(b.REPOS) == {"b", "core"}
    assert a.REPOS["a"] == str(world["root"] / "svc-a") and b.REPOS["b"] == str(world["root"] / "svc-b")
    assert a.REPOS_SOURCE == b.REPOS_SOURCE == "project"


def test_list_repos_y_citas_por_proyecto(world, monkeypatch):
    _local(monkeypatch, REPOS_ROOT=str(world["root"]))
    for name, own, other in (("alpha", "service/a.go:3", "service/b.go:3"), ("beta", "service/b.go:3", "service/a.go:3")):
        cfg = config_loader.load_config(name, projects_dir=world["projects"])
        monkeypatch.setattr(config, "REPOS", cfg.REPOS, raising=False)
        listed = registry.list_repos()
        assert own.split("/")[0] in listed or "svc-" in listed
        assert ("svc-a" in listed) == (name == "alpha") and ("svc-b" in listed) == (name == "beta")
        index = cit.RepoIndex(cfg.REPOS)
        checked = citation_check.verify(f"Ver {own} y {other}", index)
        assert f"{own} ⚠" not in checked.text and f"{other} ⚠ no existe" in checked.text
        results = cit.check_project(cfg, name)
        assert sorted(c.status for c in results) == ["inexistente", "ok"]


def test_override_local_por_alias_en_load_config(world, monkeypatch):
    _local(monkeypatch, REPOS_ROOT=str(world["root"]), REPOS_OVERRIDES={"b": str(world["elsewhere"])})
    b = config_loader.load_config("beta", projects_dir=world["projects"])
    assert b.REPOS["b"] == str(world["elsewhere"]) and b.REPOS["core"] == str(world["root"] / "svc-shared")
    index = cit.RepoIndex(b.REPOS)
    assert index.resolve("service/b.go")[0] == cit.STATUS_OK
    # 50 líneas en el override: la 40 existe; en el repo original (10 líneas) no existiría.
    assert citation_check.verify("service/b.go:40", index).missing == 0


def test_esquema_viejo_sigue_funcionando_y_doctor_avisa(world, monkeypatch, tmp_path):
    _local(monkeypatch, REPOS={"a": str(world["root"] / "svc-a")})
    cfg = config_loader.load_config("legacy", projects_dir=world["projects"])
    assert cfg.REPOS == {"a": str(world["root"] / "svc-a")} and cfg.REPOS_SOURCE == "local"
    cfg.DATA_DIR = tmp_path / "data"
    (cfg.DATA_DIR).mkdir()
    (cfg.DATA_DIR / "config_local.py").write_text('LLM_BACKEND = "anthropic"\n', encoding="utf-8")
    cfg.USE_BEDROCK = False
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    by = {c.name: c for c in doctor.run_checks(cfg)}
    assert by["Repos"].ok
    assert not by["REPOS por proyecto"].ok and not by["REPOS por proyecto"].critical
    assert "deprecado" in by["REPOS por proyecto"].detail
    assert not doctor.has_critical_failure([by["Repos"], by["REPOS por proyecto"]])

    _local(monkeypatch, REPOS_ROOT=str(world["root"]))
    cfg2 = config_loader.load_config("alpha", projects_dir=world["projects"])
    cfg2.DATA_DIR = cfg.DATA_DIR
    cfg2.USE_BEDROCK = False
    names = [c.name for c in doctor.run_checks(cfg2)]
    assert "REPOS por proyecto" not in names  # esquema nuevo: sin aviso


def test_rutas_fuera_de_repos_rechazadas(world, monkeypatch):
    _local(monkeypatch, REPOS_ROOT=str(world["root"]))
    a = config_loader.load_config("alpha", projects_dir=world["projects"])
    index = cit.RepoIndex(a.REPOS)
    assert index.resolve("../svc-b/service/b.go")[0] == cit.STATUS_OUTSIDE
    assert index.resolve("svc-b/service/b.go")[0] == cit.STATUS_MISSING  # repo de otro proyecto
    assert citation_check.verify("ver ../svc-b/service/b.go:1", index).missing == 1


def test_cli_knowledge_check_usa_los_repos_del_proyecto(world, monkeypatch):
    _local(monkeypatch, REPOS_ROOT=str(world["root"]))
    a = config_loader.load_config("alpha", projects_dir=world["projects"])
    for key in ("REPOS", "REPOS_SOURCE", "PROJECTS_DIR", "ACTIVE_PROJECT"):
        monkeypatch.setattr(config, key, getattr(a, key), raising=False)
    result = CliRunner().invoke(minka_main.app, ["knowledge", "check"])
    assert result.exit_code == 1, result.output
    assert "architecture.md: total=2 ok=1 inexistente=1" in result.output
    assert "service/b.go:3" in result.output and "service/a.go:3 ⚠" not in result.output
