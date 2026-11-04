"""`minka --new-project` completo: STACK detectado, COMPLIANCE, repos (relativos a REPOS_ROOT
o absolutos), modo no interactivo, colisión, ruta inexistente y doctor al final. Sin red."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from minka import config_loader
from minka import main as minka_main
from minka.config_loader import config
from minka.core import project_scaffold as ps


@pytest.fixture
def world(tmp_path, monkeypatch):
    # El flujo llama a set_project(), que muta `config` in place: se restaura entero al salir
    # para que el resto de la suite no herede el proyecto temporal (STACK, REPOS…).
    saved = dict(config.__dict__)
    yield _build_world(tmp_path, monkeypatch)
    config.__dict__.clear()
    config.__dict__.update(saved)


def _build_world(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    (data / "config_local.py").write_text('LLM_BACKEND = "anthropic"\n', encoding="utf-8")
    (data / ".env").write_text("", encoding="utf-8")
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "PROJECTS_DIR", data / "projects", raising=False)
    monkeypatch.setattr(config, "ACTIVE_CONTEXT_DIR", None, raising=False)
    monkeypatch.setattr(config, "REPOS_ROOT", None, raising=False)
    monkeypatch.setattr(config, "USE_BEDROCK", False, raising=False)
    # set_project (doctor del proyecto nuevo) relee config_local desde el DATA_DIR base.
    monkeypatch.setattr(config_loader._base, "DATA_DIR", data)
    monkeypatch.setattr(config_loader._base, "PROJECTS_DIR", data / "projects")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")

    clones = tmp_path / "clones"
    repos = {}
    for name, marker in (("svc-go", "go.mod"), ("svc-py", "pyproject.toml"), ("svc-node", "package.json")):
        (clones / name / "src").mkdir(parents=True)
        (clones / name / marker).write_text("x\n", encoding="utf-8")
        repos[name] = clones / name
    elsewhere = tmp_path / "elsewhere" / "svc-other"
    (elsewhere / "src").mkdir(parents=True)
    (elsewhere / "go.mod").write_text("x\n", encoding="utf-8")
    repos["other"] = elsewhere
    return dict(data=data, repos=repos, clones=clones)


def _run(args, **kw):
    return CliRunner().invoke(minka_main.app, args, **kw)


def _cfg(world, name) -> str:
    return (world["data"] / "projects" / name / "config.py").read_text(encoding="utf-8")


def test_detect_stack():
    assert ps.detect_stack(Path("/no/existe")) is None


def test_detect_stack_por_marcadores(world):
    r = world["repos"]
    assert ps.detect_stack(r["svc-go"]) == "go"
    assert ps.detect_stack(r["svc-py"]) == "python"
    assert ps.detect_stack(r["svc-node"]) == "node"
    assert ps.detect_stack(world["clones"]) is None


def test_plan_repos_relativo_absoluto(world):
    r = world["repos"]
    rel, root = ps.plan_repos({"a": str(r["svc-go"]), "b": str(r["svc-py"])}, None)
    assert rel == {"a": "svc-go", "b": "svc-py"} and root == str(world["clones"])
    rel2, root2 = ps.plan_repos({"a": str(r["svc-go"])}, str(world["clones"]))
    assert rel2 == {"a": "svc-go"} and root2 is None
    abs_, root3 = ps.plan_repos({"a": str(r["svc-go"]), "o": str(r["other"])}, None)
    assert abs_ == {"a": str(r["svc-go"]), "o": str(r["other"])} and root3 is None
    # Raíz configurada real pero distinta → absolutas y nada que fijar.
    abs2, root4 = ps.plan_repos({"a": str(r["svc-go"])}, str(r["other"].parent))
    assert abs2["a"] == str(r["svc-go"]) and root4 is None
    # Raíz configurada inexistente (placeholder de la plantilla) → como si no existiera.
    rel3, root5 = ps.plan_repos({"a": str(r["svc-go"])}, "/no/existe/raiz")
    assert rel3 == {"a": "svc-go"} and root5 == str(world["clones"])


def test_no_interactivo_python_detectado_y_relativo(world):
    result = _run(["--new-project", "py-demo", "--repo", f"app={world['repos']['svc-py']}", "--yes"])
    assert result.exit_code == 0, result.output
    text = _cfg(world, "py-demo")
    assert 'STACK = "python"' in text and "COMPLIANCE = []" in text
    assert "'app': 'svc-py'" in text and "svc-pos" not in text
    local = (world["data"] / "config_local.py").read_text(encoding="utf-8")
    assert "REPOS_ROOT = " in local and str(world["clones"]) in local
    assert "REPOS_ROOT" in result.output  # el panel de Rich puede partir la frase
    # doctor del proyecto y siguiente paso, sin lanzar el indexado
    assert "✓ Repos" in result.output and "--index-repos --project py-demo" in result.output
    assert "Proyecto activo" in result.output


def test_no_interactivo_go_con_pci_y_node(world):
    result = _run(["--new-project", "go-demo", "--repo", f"pos={world['repos']['svc-go']}", "--compliance", "pci", "--yes"])
    assert result.exit_code == 0, result.output
    text = _cfg(world, "go-demo")
    assert 'STACK = "go"' in text and 'COMPLIANCE = ["pci"]' in text
    result = _run(["--new-project", "node-demo", "--repo", f"web={world['repos']['svc-node']}", "--yes"])
    assert result.exit_code == 0, result.output
    assert 'STACK = "node"' in _cfg(world, "node-demo")


def test_repos_en_carpetas_distintas_quedan_absolutos(world):
    r = world["repos"]
    result = _run(
        ["--new-project", "mix", "--repo", f"a={r['svc-go']}", "--repo", f"o={r['other']}", "--stack", "go", "--yes"]
    )
    assert result.exit_code == 0, result.output
    text = _cfg(world, "mix")
    assert str(r["svc-go"]) in text and str(r["other"]) in text
    assert "REPOS_ROOT" not in (world["data"] / "config_local.py").read_text(encoding="utf-8")


def test_stack_explicito_gana_y_stack_invalido_falla(world):
    result = _run(["--new-project", "forzado", "--repo", f"app={world['repos']['svc-py']}", "--stack", "go", "--yes"])
    assert result.exit_code == 0 and 'STACK = "go"' in _cfg(world, "forzado")
    result = _run(["--new-project", "malo", "--repo", f"app={world['repos']['svc-py']}", "--stack", "rust", "--yes"])
    assert result.exit_code == 2 and "STACK inválido" in result.output
    assert not (world["data"] / "projects" / "malo").exists()


def test_colision_no_sobrescribe(world):
    assert _run(["--new-project", "dup", "--repo", f"app={world['repos']['svc-py']}", "--yes"]).exit_code == 0
    before = _cfg(world, "dup")
    result = _run(["--new-project", "dup", "--repo", f"app={world['repos']['svc-go']}", "--yes"])
    assert result.exit_code == 1 and "ya existe" in result.output
    assert _cfg(world, "dup") == before


def test_ruta_inexistente_no_crea_nada(world):
    result = _run(["--new-project", "roto", "--repo", "app=/no/existe", "--yes"])
    assert result.exit_code == 2 and "no existe" in result.output
    assert not (world["data"] / "projects" / "roto").exists()
    result = _run(["--new-project", "roto", "--repo", "sin-igual", "--yes"])
    assert result.exit_code == 2 and "alias=ruta" in result.output


def test_sin_nombre_con_yes_falla(world):
    result = _run(["--new-project", "--yes"])
    assert result.exit_code == 2


def test_interactivo_pregunta_y_valida_rutas(world):
    entrada = "\n".join(
        [
            "app",  # alias
            "/no/existe",  # ruta inválida → vuelve a pedir
            str(world["repos"]["svc-py"]),  # ruta válida
            "",  # fin de repos
            "",  # STACK: Enter = detectado (python)
            "s",  # PCI sí
        ]
    ) + "\n"
    result = _run(["--new-project", "inter"], input=entrada)
    assert result.exit_code == 0, result.output
    text = _cfg(world, "inter")
    assert 'STACK = "python"' in text and 'COMPLIANCE = ["pci"]' in text and "'app': 'svc-py'" in text
    assert "esa ruta no existe" in result.output and "detectado: python" in result.output


def test_interactivo_go_pregunta_core_module(world):
    entrada = "\n".join(["pos", str(world["repos"]["svc-go"]), "", "", "n", "svc-core"]) + "\n"
    result = _run(["--new-project", "gointer"], input=entrada)
    assert result.exit_code == 0, result.output
    text = _cfg(world, "gointer")
    assert 'STACK = "go"' in text and "COMPLIANCE = []" in text and 'CORE_MODULE_NAME = "svc-core"' in text


def test_placeholder_de_repos_root_se_reemplaza(world, monkeypatch):
    """`minka init` deja REPOS_ROOT con una carpeta de ejemplo inexistente: --new-project la
    sustituye por la carpeta real y deja los repos relativos."""
    local = world["data"] / "config_local.py"
    local.write_text('LLM_BACKEND = "anthropic"\nREPOS_ROOT = "~/ruta/donde/clonas/los/repos"\n', encoding="utf-8")
    result = _run(["--new-project", "ph", "--repo", f"app={world['repos']['svc-py']}", "--yes"])
    assert result.exit_code == 0, result.output
    text = local.read_text(encoding="utf-8")
    assert text.count("REPOS_ROOT = ") == 1 and str(world["clones"]) in text and "~/ruta" not in text
    assert "'app': 'svc-py'" in _cfg(world, "ph")
    # Una raíz real distinta se respeta: los repos quedan absolutos.
    other = world["repos"]["other"].parent
    local.write_text(f'LLM_BACKEND = "anthropic"\nREPOS_ROOT = {str(other)!r}\n', encoding="utf-8")
    monkeypatch.setattr(config, "REPOS_ROOT", str(other), raising=False)
    result = _run(["--new-project", "ph2", "--repo", f"app={world['repos']['svc-py']}", "--yes"])
    assert result.exit_code == 0, result.output
    assert str(world["repos"]["svc-py"]) in _cfg(world, "ph2")
    assert local.read_text(encoding="utf-8").count("REPOS_ROOT = ") == 1


def test_create_project_sin_repos_mantiene_plantilla(world):
    r = ps.create_project("plain")
    text = _cfg(world, "plain")
    assert "# STACK" in text and "# COMPLIANCE" in text and "svc-pos" in text
    assert r["repos"] == {} and r["repos_root"] is None
    assert any("REPOS_ROOT" in s for s in r["next_steps"])
