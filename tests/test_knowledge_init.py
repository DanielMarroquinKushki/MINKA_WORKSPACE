"""`minka knowledge init` (core/knowledge/extractor.py): parte determinista para python y go,
exclusiones y escaneo de secretos, borrador + diff, --apply sin sobrescribir. Sin red ni modelo."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from minka import config_loader
from minka import main as minka_main
from minka.config_loader import config
from minka.core.knowledge import extractor as ex
from minka.core.tools.analysis import citations as cit


def _git_init(repo: Path) -> None:
    for args in (("init", "-q", "-b", "main"), ("config", "user.email", "t@example.com"), ("config", "user.name", "t"),
                 ("add", "."), ("commit", "-q", "-m", "inicio")):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _py_repo(root: Path) -> Path:
    repo = root / "svc-py"
    (repo / "app" / "core").mkdir(parents=True)
    (repo / "app" / "api").mkdir()
    (repo / "app" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "app" / "core" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "app" / "api" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "app" / "core" / "tokens.py").write_text("import os\n\ndef resolve_token():\n    return os.getenv('T', '')\n", encoding="utf-8")
    (repo / "app" / "api" / "handler.py").write_text("import json\nfrom app.core import tokens\nfrom . import helpers\n\ndef handle():\n    return json.dumps(tokens.resolve_token())\n", encoding="utf-8")
    (repo / "app" / "api" / "helpers.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    (repo / "pyproject.toml").write_text('[project]\nname = "svc-py"\n', encoding="utf-8")
    (repo / "README.md").write_text("# svc-py\nServicio de ejemplo.\n", encoding="utf-8")
    (repo / ".env").write_text("SUPER_SECRET_VALUE_XYZ=abc\n", encoding="utf-8")
    (repo / "server.pem").write_text("PEM_SECRET_BODY_123\n", encoding="utf-8")
    (repo / ".venv" / "lib").mkdir(parents=True)
    (repo / ".venv" / "lib" / "ignorado.py").write_text("import os\n", encoding="utf-8")
    _git_init(repo)
    return repo


def _go_repo(root: Path, with_gomod: bool = True) -> Path:
    repo = root / ("svc-go" if with_gomod else "svc-go-nomod")
    (repo / "service").mkdir(parents=True)
    (repo / "gateway").mkdir()
    if with_gomod:
        (repo / "go.mod").write_text("module example.com/svc\n\ngo 1.22\n", encoding="utf-8")
    (repo / "service" / "charge.go").write_text(
        'package service\n\nimport (\n\t"fmt"\n\t"example.com/svc/gateway"\n)\n\nfunc Charge() { fmt.Println(gateway.Send()) }\n',
        encoding="utf-8",
    )
    (repo / "gateway" / "sender.go").write_text('package gateway\n\nfunc Send() string { return "ok" }\n', encoding="utf-8")
    (repo / "vendor" / "x").mkdir(parents=True)
    (repo / "vendor" / "x" / "v.go").write_text("package x\n", encoding="utf-8")
    (repo / "README.md").write_text("# svc-go\n", encoding="utf-8")
    _git_init(repo)
    return repo


@pytest.fixture
def world(tmp_path, monkeypatch):
    saved = dict(config.__dict__)
    data = tmp_path / "data"
    projects = data / "projects"
    for name, stack, repo_fn in (("pyproj", "python", _py_repo), ("goproj", "go", _go_repo)):
        repo = repo_fn(tmp_path)
        (projects / name / "knowledge" / "extracted").mkdir(parents=True)
        (projects / name / "prompts").mkdir()
        (projects / name / "config.py").write_text(
            f'STACK = "{stack}"\nREPOS = {{"{repo.name}": {str(repo)!r}}}\nAGENT_KNOWLEDGE = {{}}\n', encoding="utf-8"
        )
    nomod = _go_repo(tmp_path, with_gomod=False)
    (projects / "gonomod" / "knowledge" / "extracted").mkdir(parents=True)
    (projects / "gonomod" / "config.py").write_text(f'STACK = "go"\nREPOS = {{"svc": {str(nomod)!r}}}\n', encoding="utf-8")
    (projects / "nodeproj" / "knowledge").mkdir(parents=True)
    (projects / "nodeproj" / "config.py").write_text('STACK = "node"\nREPOS = {}\n', encoding="utf-8")
    monkeypatch.setattr(config_loader, "_load_local_vars", lambda d: {"LLM_BACKEND": "anthropic"})
    monkeypatch.setattr(config_loader._base, "PROJECTS_DIR", projects)
    monkeypatch.setattr(config_loader._base, "DATA_DIR", data)
    monkeypatch.setattr(config_loader._base, "OUTPUT_DIR", data / "output")  # set_project relee la base
    monkeypatch.setattr(config, "PROJECTS_DIR", projects, raising=False)
    monkeypatch.setattr(config, "DATA_DIR", data, raising=False)
    monkeypatch.setattr(config, "OUTPUT_DIR", data / "output", raising=False)
    yield dict(data=data, projects=projects, py=tmp_path / "svc-py", go=tmp_path / "svc-go", nomod=nomod)
    config.__dict__.clear()
    config.__dict__.update(saved)


def _run(args, **kw):
    return CliRunner().invoke(minka_main.app, args, **kw)


def _draft(world, name):
    return (world["data"] / "output" / "knowledge_draft" / name / "architecture.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# extractor (unidad)
# ---------------------------------------------------------------------------


def test_exclusiones_y_binarios(world, tmp_path):
    repo = world["py"]
    assert ex.safe_read(repo / ".env", repo) is None
    assert ex.safe_read(repo / "server.pem", repo) is None
    assert ex.safe_read(repo / ".venv" / "lib" / "ignorado.py", repo) is None
    assert ex.safe_read(repo / "README.md", repo) is not None
    binario = repo / "blob.py"
    binario.write_bytes(b"\x00\x01\x02 binario")
    assert ex.safe_read(binario, repo) is None


def test_scan_secrets_lista_ruta_y_patron_sin_valor():
    text = "x = 1\nkey = 'AKIAABCDEFGHIJKLMNOP'\npassword = \"supersecreta123\"\n-----BEGIN PRIVATE KEY-----\n"
    hits = ex.scan_secrets(text, "repo/cfg.py")
    assert hits == ["repo/cfg.py:2 (AWS_ACCESS_KEY)", "repo/cfg.py:3 (CREDENTIAL_ASSIGN)", "repo/cfg.py:4 (PRIVATE_KEY)"]
    assert "AKIAABCDEFGHIJKLMNOP" not in " ".join(hits) and "supersecreta" not in " ".join(hits)
    assert ex.scan_secrets("def f():\n    return 'ok'\n", "a.py") == []
    extra = [("CUSTOM", "ALTA", r"MI_PATRON_PROPIO", "regla del proyecto")]
    assert ex.scan_secrets("x = MI_PATRON_PROPIO\n", "a.py", extra) == ["a.py:1 (CUSTOM)"]


def test_extract_python_conteos_imports_y_citas(world):
    cfg = config_loader.load_config("pyproj")
    draft = ex.build_draft(cfg, "pyproj")
    r = draft.repos[0]
    assert r.stack == "python" and r.ext_counts[".py"] == 6 and r.ext_counts[".md"] == 1 and r.ext_counts[".toml"] == 1
    assert "app/core" in r.modules and "app/api" in r.modules
    api = r.modules["app/api"]
    targets = {t for t, _ in api.imports}
    assert targets == {"app.core", "."}  # json es externo; from . import helpers es relativo
    assert all(ex.VERIFY not in c for _, c in api.imports)
    assert any(c == "svc-py/app/api/handler.py:2" for _, c in api.imports)
    assert r.readme == "svc-py/README.md:1" and r.manifest == "svc-py/pyproject.toml:1"
    assert len(r.git_log) == 1 and r.commit != "sin-git"
    assert r.files_read == 6 + 2  # 6 .py + README + manifiesto


def test_extract_go_imports_del_modulo_y_sin_gomod(world):
    cfg = config_loader.load_config("goproj")
    r = ex.build_draft(cfg, "goproj").repos[0]
    assert r.module_name == "example.com/svc" and r.ext_counts[".go"] == 2  # vendor excluido
    assert r.modules["service"].imports == [("gateway", "svc-go/service/charge.go:5")]
    assert r.modules["gateway"].imports == []
    cfg2 = config_loader.load_config("gonomod")
    r2 = ex.build_draft(cfg2, "gonomod").repos[0]
    assert r2.module_name is None and any(ex.VERIFY in n for n in r2.notes)
    assert r2.modules["service"].imports and all(ex.VERIFY in c for _, c in r2.modules["service"].imports)


def test_todas_las_citas_del_borrador_pasan_repo_index_y_sin_juicios(world, tmp_path):
    cfg = config_loader.load_config("pyproj")
    draft = ex.build_draft(cfg, "pyproj")
    path = tmp_path / "architecture.md"
    path.write_text(draft.architecture, encoding="utf-8")
    results = cit.check_doc(path, cit.RepoIndex(cfg.REPOS))
    assert results and all(not c.broken for c in results)
    assert ex.contains_judgement(draft.architecture) == []
    assert "SUPER_SECRET" not in draft.architecture and "PEM_SECRET" not in draft.architecture
    assert ".env" not in draft.architecture and "server.pem" not in draft.architecture
    assert "Generado:" in draft.architecture and f"svc-py@{draft.repos[0].commit}" in draft.architecture
    assert "fuente: svc-py@" in draft.architecture
    assert draft.sources["docs"]["extracted/architecture.md"]["sources"]["svc-py"]["commit"] == draft.repos[0].commit


def test_contains_judgement_detecta_estados():
    assert ex.contains_judgement("# t\n\n\n\n\n\n- El reverso está vigente y el bug cerrado") == ["vigente", "cerrado", "bug"]


def test_secreto_aborta_sin_escribir(world):
    (world["py"] / "app" / "cfg.py").write_text("KEY = 'AKIAABCDEFGHIJKLMNOP'\n", encoding="utf-8")
    cfg = config_loader.load_config("pyproj")
    with pytest.raises(ex.SecretFound) as info:
        ex.build_draft(cfg, "pyproj")
    assert info.value.hits == ["svc-py/app/cfg.py:1 (AWS_ACCESS_KEY)"]
    assert not (world["data"] / "output").exists()


def test_stack_node_no_soportado(world):
    cfg = config_loader.load_config("nodeproj")
    with pytest.raises(ex.UnsupportedStack):
        ex.build_draft(cfg, "nodeproj")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_borrador_y_diff(world):
    result = _run(["knowledge", "init", "--project", "pyproj"])
    assert result.exit_code == 0, result.output
    out = world["data"] / "output" / "knowledge_draft" / "pyproj"
    assert (out / "architecture.md").is_file() and (out / "_sources.json").is_file() and (out / "report.txt").is_file()
    assert "modulos=" in result.output and "diff vs knowledge/extracted/architecture.md" in result.output
    assert "no existe todavía" in result.output and "--apply" in result.output
    assert not (world["projects"] / "pyproj" / "knowledge" / "extracted" / "architecture.md").exists()
    json.loads((out / "_sources.json").read_text(encoding="utf-8"))


def test_cli_apply_crea_y_luego_escribe_new(world):
    result = _run(["knowledge", "init", "--project", "pyproj", "--apply", "--yes"])
    assert result.exit_code == 0, result.output
    extracted = world["projects"] / "pyproj" / "knowledge" / "extracted"
    assert (extracted / "architecture.md").is_file() and (extracted / "_sources.json").is_file()
    assert "rotas: 0" in result.output  # knowledge check al final
    before = (extracted / "architecture.md").read_text(encoding="utf-8")
    result = _run(["knowledge", "init", "--project", "pyproj", "--apply", "--yes"])
    assert result.exit_code == 0, result.output
    assert (extracted / "architecture.md.new").is_file() and (extracted / "_sources.json.new").is_file()
    assert (extracted / "architecture.md").read_text(encoding="utf-8") == before
    assert ".new" in result.output


def test_cli_apply_pide_confirmacion(world):
    result = _run(["knowledge", "init", "--project", "pyproj", "--apply"], input="n\n")
    assert result.exit_code == 0 and "Sin cambios" in result.output
    assert not (world["projects"] / "pyproj" / "knowledge" / "extracted" / "architecture.md").exists()


def test_cli_secreto_aborta_con_ruta_sin_valor(world):
    (world["py"] / "app" / "cfg.py").write_text("TOKEN = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.abcdefghijklmnop'\n", encoding="utf-8")
    result = _run(["knowledge", "init", "--project", "pyproj"])
    assert result.exit_code == 3
    assert "svc-py/app/cfg.py:1 (JWT)" in result.output and "eyJhbGci" not in result.output
    assert not (world["data"] / "output" / "knowledge_draft").exists()


def test_cli_node_mensaje_claro(world):
    result = _run(["knowledge", "init", "--project", "nodeproj"])
    assert result.exit_code == 2 and "no soportado aún" in result.output


def test_cli_go_apply_knowledge_check_cero_rotas(world):
    result = _run(["knowledge", "init", "--project", "goproj", "--apply", "--yes"])
    assert result.exit_code == 0, result.output
    assert "rotas: 0" in result.output
    text = _draft(world, "goproj")
    assert "svc-go/service/charge.go:5" in text and "vendor" not in text
