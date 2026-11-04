"""Tests de core/tools/analysis/citations.py y de `minka knowledge check`.

Repos de mentira en tmp_path (sin git, sin red, sin DATA_DIR real): uno se referencia
por symlink, como hace el workspace del equipo.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from minka import main as minka_main
from minka.config_loader import config
from minka.core.tools.analysis import citations as cit

PAYMENTS_GO = "\n".join(
    [
        "package service",
        "",
        "// ProcessPayment valida y envía la autorización.",
        "func ProcessPayment() {}",
        "",
        "func helper() {}",
        "",
        "func BuildReverse() {}",
    ]
)


@pytest.fixture
def repos(tmp_path: Path) -> dict[str, Path]:
    pagos = tmp_path / "real" / "usrv-pagos"
    (pagos / "service").mkdir(parents=True)
    (pagos / "tool").mkdir()
    (pagos / "vendor" / "x").mkdir(parents=True)
    (pagos / "service" / "payments.go").write_text(PAYMENTS_GO, encoding="utf-8")
    (pagos / "tool" / "common.go").write_text("package tool\n", encoding="utf-8")
    (pagos / "vendor" / "x" / "common.go").write_text("package x\n", encoding="utf-8")

    ledger = tmp_path / "real" / "usrv-ledger"
    (ledger / "tool").mkdir(parents=True)
    (ledger / "tool" / "common.go").write_text("package tool\n", encoding="utf-8")
    (ledger / "serverless.yml").write_text("functions:\n  a: 1\n", encoding="utf-8")

    outside = tmp_path / "outside.go"
    outside.write_text("package outside\n", encoding="utf-8")
    (pagos / "escape.go").symlink_to(outside)

    links = tmp_path / "links"
    links.mkdir()
    (links / "usrv-pagos").symlink_to(pagos)
    return {"pagos": links / "usrv-pagos", "ledger": ledger}


@pytest.fixture
def index(repos) -> cit.RepoIndex:
    return cit.RepoIndex({k: str(v) for k, v in repos.items()})


def _doc(tmp_path: Path, name: str, text: str) -> Path:
    doc = tmp_path / name
    doc.write_text(text, encoding="utf-8")
    return doc


def _statuses(items: list[cit.Citation]) -> dict[str, str]:
    return {c.raw: c.status for c in items}


# ---------------------------------------------------------------------------
# resolución
# ---------------------------------------------------------------------------


def test_repo_index_resuelve_symlinks(index, repos):
    root = index.roots["pagos"]
    assert not root.is_symlink()
    assert root == repos["pagos"].resolve()


def test_resuelve_literal_alias_y_nombre_de_repo(index):
    for cited in ("service/payments.go", "PAGOS/service/payments.go", "usrv-pagos/service/payments.go"):
        status, matches = index.resolve(cited)
        assert status == cit.STATUS_OK, cited
        assert matches[0][0] == "pagos"


def test_literal_gana_sobre_alias_homonimo(tmp_path, repos):
    # Un directorio llamado como la clave del repo dentro de otro repo no se confunde con el alias.
    (repos["ledger"] / "pagos").mkdir()
    (repos["ledger"] / "pagos" / "log.go").write_text("package pagos\n", encoding="utf-8")
    index = cit.RepoIndex({k: str(v) for k, v in repos.items()})
    status, matches = index.resolve("pagos/log.go")
    assert status == cit.STATUS_OK
    assert matches[0][0] == "ledger"


def test_basename_unico_y_ambiguo(index):
    status, matches = index.resolve("payments.go")
    assert status == cit.STATUS_OK and matches[0][0] == "pagos"
    status, matches = index.resolve("tool/common.go")
    assert status == cit.STATUS_AMBIGUOUS
    assert sorted(k for k, _ in matches) == ["ledger", "pagos"]


def test_vendor_no_entra_al_indice(index):
    # tool/common.go existe en vendor/ de pagos; el índice lo ignora.
    assert all("vendor" not in p.parts for _, p in index._index()["common.go"])


def test_rechaza_rutas_fuera_de_repos(index):
    assert index.resolve("../outside.go")[0] == cit.STATUS_OUTSIDE
    assert index.resolve("escape.go")[0] == cit.STATUS_MISSING  # symlink que escapa
    assert index.resolve("PAGOS/../outside.go")[0] == cit.STATUS_OUTSIDE


def test_elidida_e_inexistente(index):
    assert index.resolve("usrv-pagos/.../payments.go")[0] == cit.STATUS_ELIDED
    assert index.resolve("service/gone.go")[0] == cit.STATUS_MISSING
    assert index.resolve("PAGOS/service/gone.go")[0] == cit.STATUS_MISSING


def test_repos_vacios_o_sin_ruta():
    assert cit.RepoIndex({"pagos": "", "ledger": "/no/existe"}).empty
    assert cit.RepoIndex(None).empty


# ---------------------------------------------------------------------------
# chequeo de documentos
# ---------------------------------------------------------------------------


def test_check_doc_lineas_y_simbolos(tmp_path, index):
    doc = _doc(
        tmp_path,
        "architecture.md",
        "\n".join(
            [
                "- `ProcessPayment()` vive en service/payments.go:4",
                "- `BuildReverse` se citaba en service/payments.go:1",
                "- `Nothing` estaba en service/payments.go:2",
                "- fuera de rango: service/payments.go:99",
                "- rango ok: service/payments.go:3-5 con `ProcessPayment`",
                "- sin línea: usrv-ledger/serverless.yml",
                "- R-07 borrado: service/gone.go:10",
                "- ambigua: tool/common.go:1",
            ]
        ),
    )
    items = cit.check_doc(doc, index)
    st = _statuses(items)
    assert st["service/payments.go:4"] == cit.STATUS_OK
    assert st["service/payments.go:1"] == cit.STATUS_SYMBOL_MOVED
    assert st["service/payments.go:2"] == cit.STATUS_SYMBOL_ABSENT
    assert st["service/payments.go:99"] == cit.STATUS_OUT_OF_RANGE
    assert st["service/payments.go:3-5"] == cit.STATUS_OK
    assert st["usrv-ledger/serverless.yml"] == cit.STATUS_OK
    assert st["service/gone.go:10"] == cit.STATUS_MISSING
    assert st["tool/common.go:1"] == cit.STATUS_AMBIGUOUS

    moved = next(c for c in items if c.status == cit.STATUS_SYMBOL_MOVED)
    assert "línea 8" in moved.note
    assert next(c for c in items if c.raw == "service/gone.go:10").risk_id == "R-07"

    # rotas = inexistente + fuera_rango; los avisos no cuentan
    assert sorted(c.raw for c in cit.broken(items)) == ["service/gone.go:10", "service/payments.go:99"]
    assert all(not c.broken for c in items if c.warning)


def test_simbolo_sin_backticks_no_genera_aviso(tmp_path, index):
    doc = _doc(tmp_path, "notes.md", "ProcessPayment se movió, dice service/payments.go:1")
    assert cit.check_doc(doc, index)[0].status == cit.STATUS_OK


def test_find_docs_incluye_extracted_y_catalogo(tmp_path):
    know = tmp_path / "knowledge"
    (know / "extracted").mkdir(parents=True)
    (know / "team").mkdir()
    (know / "extracted" / "b.md").write_text("", encoding="utf-8")
    (know / "extracted" / "a.md").write_text("", encoding="utf-8")
    (know / "extracted" / "risk_catalog.md").write_text("", encoding="utf-8")
    (know / "team" / "risk_catalog.md").write_text("", encoding="utf-8")
    (know / "team" / "session_log.md").write_text("", encoding="utf-8")
    names = [p.relative_to(know).as_posix() for p in cit.find_docs(know)]
    assert names == ["extracted/a.md", "extracted/b.md", "extracted/risk_catalog.md", "team/risk_catalog.md"]


def test_risk_id_de_la_seccion_cubre_sus_citas(tmp_path, index):
    doc = _doc(
        tmp_path,
        "risk_catalog.md",
        "\n".join(
            [
                "### R-02 — llaves en el repo",
                "- service/gone.go:1",
                "- R-09 explícito en la línea: service/payments.go:4",
                "## Cierre sin id",
                "- service/payments.go:4",
            ]
        ),
    )
    ids = [c.risk_id for c in cit.check_doc(doc, index)]
    assert ids == ["R-02", "R-09", ""]


def test_format_report_conteos_por_documento(tmp_path, index):
    doc = _doc(tmp_path, "risk_catalog.md", "R-01 en service/gone.go:3\nok service/payments.go:4\n")
    text = cit.format_report(cit.check_doc(doc, index))
    assert "risk_catalog.md: total=2 ok=1 inexistente=1" in text
    assert "risk_catalog.md:1 R-01 [inexistente] service/gone.go:3" in text
    assert "rotas: 1" in text
    assert cit.format_report([]).startswith("Sin citas")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _project(tmp_path: Path, repos: dict[str, Path], doc_text: str) -> SimpleNamespace:
    projects = tmp_path / "projects"
    extracted = projects / "demo" / "knowledge" / "extracted"
    extracted.mkdir(parents=True)
    (extracted / "architecture.md").write_text(doc_text, encoding="utf-8")
    return SimpleNamespace(
        PROJECTS_DIR=projects, ACTIVE_PROJECT="demo", REPOS={k: str(v) for k, v in repos.items()}
    )


def _apply(cfg: SimpleNamespace, monkeypatch) -> None:
    for key in ("PROJECTS_DIR", "ACTIVE_PROJECT", "REPOS"):
        monkeypatch.setattr(config, key, getattr(cfg, key), raising=False)


def test_cli_exit_1_con_rotas(tmp_path, repos, monkeypatch):
    _apply(_project(tmp_path, repos, "ver service/gone.go:1 y service/payments.go:4\n"), monkeypatch)
    result = CliRunner().invoke(minka_main.app, ["knowledge", "check"])
    assert result.exit_code == 1, result.output
    assert "architecture.md: total=2 ok=1 inexistente=1" in result.output


def test_cli_exit_0_solo_avisos(tmp_path, repos, monkeypatch):
    _apply(_project(tmp_path, repos, "`BuildReverse` en service/payments.go:1 y tool/common.go\n"), monkeypatch)
    result = CliRunner().invoke(minka_main.app, ["knowledge", "check"])
    assert result.exit_code == 0, result.output
    assert "simbolo_movido=1" in result.output and "ambigua=1" in result.output


def test_cli_project_cambia_el_activo(tmp_path, repos, monkeypatch):
    cfg = _project(tmp_path, repos, "service/payments.go:4\n")
    _apply(cfg, monkeypatch)
    monkeypatch.setattr(config, "ACTIVE_PROJECT", None, raising=False)
    calls = []

    def fake_set_project(name):
        calls.append(name)
        config.ACTIVE_PROJECT = "demo"

    monkeypatch.setattr("minka.config_loader.set_project", fake_set_project)
    result = CliRunner().invoke(minka_main.app, ["knowledge", "check", "--project", "demo"])
    assert result.exit_code == 0, result.output
    assert calls == ["demo"]


def test_cli_sin_proyecto_ni_repos(tmp_path, repos, monkeypatch):
    cfg = _project(tmp_path, repos, "service/payments.go:4\n")
    _apply(cfg, monkeypatch)
    monkeypatch.setattr(config, "ACTIVE_PROJECT", None, raising=False)
    assert CliRunner().invoke(minka_main.app, ["knowledge", "check"]).exit_code == 2
    monkeypatch.setattr(config, "ACTIVE_PROJECT", "demo", raising=False)
    monkeypatch.setattr(config, "REPOS", {"pagos": ""}, raising=False)
    assert CliRunner().invoke(minka_main.app, ["knowledge", "check"]).exit_code == 2
