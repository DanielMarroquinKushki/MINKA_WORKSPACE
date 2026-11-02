"""Alias de REPOS en path/repo_path: rutas reales (symlinks resueltos), sin rutas arbitrarias."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from minka.config_loader import config
from minka.core.agents.report_agent import ReportAgent
from minka.core.tools import registry
from minka.core.tools.registry import ToolRegistry, list_repos, resolve_repo_paths

OUTSIDE = "fuera de los repos configurados; usa list_repos"


@pytest.fixture
def entorno(tmp_path, monkeypatch):
    """repo real + symlink en repos/ (como el workspace), proyecto, output y knowledge."""
    real = tmp_path / "real" / "pos"
    (real / "service").mkdir(parents=True)
    (real / "service" / "a.go").write_text("package service\n")
    (tmp_path / "repos").mkdir()
    (tmp_path / "repos" / "pos").symlink_to(real)
    afuera = tmp_path / "afuera"
    afuera.mkdir()
    for key, value in {
        "REPOS": {"pos": str(tmp_path / "repos" / "pos"), "vacio": ""},
        "ACTIVE_PROJECT": "demo",
        "PROJECTS_DIR": tmp_path / "projects",
        "OUTPUT_DIR": tmp_path / "output",
        "KNOWLEDGE_DIR": tmp_path / "knowledge",
        "DATA_DIR": tmp_path,
    }.items():
        monkeypatch.setattr(config, key, value)
    return SimpleNamespace(tmp=tmp_path, real=real.resolve(), afuera=afuera)


def _resolve(tool, value, key="path"):
    return resolve_repo_paths(tool, {key: value}, config.REPOS)


def test_alias_y_subruta_van_a_la_ruta_real(entorno):
    assert _resolve("scan_directory", "pos") == ({"path": str(entorno.real)}, None)
    kw, err = _resolve("read_file", "pos/service/a.go")
    assert err is None and kw["path"] == str(entorno.real / "service" / "a.go")
    kw, err = _resolve("git_log", "pos", key="repo_path")
    assert err is None and kw["repo_path"] == str(entorno.real)


def test_ruta_absoluta_dentro_por_symlink_o_real_se_acepta(entorno):
    via_symlink = str(entorno.tmp / "repos" / "pos" / "service" / "a.go")
    kw, err = _resolve("read_file", via_symlink)
    assert err is None and kw["path"] == str(entorno.real / "service" / "a.go")
    assert _resolve("read_file", str(entorno.real / "service"))[1] is None


def test_ruta_fuera_se_rechaza(entorno):
    kw, err = _resolve("read_file", str(entorno.afuera / "x.txt"))
    assert OUTSIDE in err and kw == {"path": str(entorno.afuera / "x.txt")}


def test_escape_con_puntos_se_rechaza(entorno):
    kw, err = _resolve("read_file", "pos/../../afuera/x.txt")
    assert err and "sale del repo 'pos'" in err and OUTSIDE in err


def test_symlinks_dentro_del_repo(entorno):
    (entorno.real / "link_afuera").symlink_to(entorno.afuera)
    (entorno.real / "link_adentro").symlink_to(entorno.real / "service")
    assert OUTSIDE in _resolve("read_file", "pos/link_afuera/x.txt")[1]
    assert OUTSIDE in _resolve("read_file", str(entorno.real / "link_afuera" / "x.txt"))[1]
    kw, err = _resolve("read_file", "pos/link_adentro/a.go")
    assert err is None and kw["path"] == str(entorno.real / "service" / "a.go")


def test_repos_vacio_no_bloquea(entorno, monkeypatch):
    monkeypatch.setattr(config, "REPOS", {"pos": "", "hsm": ""})
    kwargs = {"path": "/etc/hosts", "content": "x"}
    assert resolve_repo_paths("write_file", kwargs, config.REPOS) == (kwargs, None)
    assert "No hay repos configurados" in list_repos()


def test_datos_legitimos_fuera_de_repos(entorno):
    decisions = entorno.tmp / "projects" / "demo" / "knowledge" / "team" / "decisions_log.md"
    assert _resolve("write_file", str(decisions))[1] is None
    assert _resolve("write_file", str(entorno.tmp / "output" / "reports" / "r.md"))[1] is None
    generico = str(entorno.tmp / "knowledge" / "professional" / "x.md")
    assert _resolve("read_file", generico)[1] is None
    assert OUTSIDE in _resolve("write_file", generico)[1]  # el knowledge del paquete no se escribe


def test_list_repos_automatica_para_todos_los_agentes(entorno, monkeypatch):
    monkeypatch.setattr(config, "AGENT_TOOLS", {"report_agent": ["read_file"], "x_agent": []})
    for agent in ("report_agent", "x_agent", "sin_perfil"):
        names = [t["name"] for t in ToolRegistry.get_tools_for_agent(agent)]
        assert names.count("list_repos") == 1
    salida = list_repos()
    assert f"pos → {entorno.real} (existe: sí)" in salida and "vacio" not in salida


def test_alias_se_resuelve_antes_del_approval_gate(entorno, monkeypatch):
    vistos = []

    class GateFalso:
        def request(self, path, content, reason):
            vistos.append(path)
            return False  # rechazado: no escribe

    monkeypatch.setattr(registry, "_get_gate", lambda: GateFalso())
    agente = object.__new__(ReportAgent)  # sin __init__: no crea cliente LLM
    agente.config = config
    bloque = SimpleNamespace(
        type="tool_use", name="write_file", id="t1",
        input={"path": "pos/service/nuevo.go", "content": "package service\n"},
    )
    agente._execute_tools(SimpleNamespace(content=[bloque]))
    assert vistos == [str(entorno.real / "service" / "nuevo.go")]
    assert not (entorno.real / "service" / "nuevo.go").exists()
    assert Path(vistos[0]).parent == entorno.real / "service"
