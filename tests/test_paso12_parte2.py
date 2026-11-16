"""Paso 12, parte 2 — símbolo cerca de la cita, antigüedad del contexto (team/) y rama por repo.
Sin red: repos git temporales, agentes falsos, fechas fijas."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from minka import main as minka_main
from minka.config_loader import config
from minka.core.evaluator import quality_evals as qe
from minka.core.evaluator.regression_evals import EvalCase
from minka.core.orchestrator import citation_check as cit_check
from minka.core.orchestrator import claim_check as cc
from minka.core.orchestrator.orchestrator import Orchestrator
from minka.core.tools.analysis import citations as cit
from minka.core.tools.analysis.citations import RepoIndex
from minka.core.tools.git import branches as br

ARCH = "architecture_agent"
TODAY = "2026-11-16"
GO = (
    "package main\n\nimport \"time\"\n\n// ResolveToken renueva el token\n"
    "func ResolveToken() time.Duration {\n\treturn 5 * time.Minute\n}\n\nconst SessionTTL = 1800\n"
    + "\n" * 49
    + "func FarSymbol() {}\n"  # línea 60
)


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _commit(repo, name, text="x\n", msg="c"):
    (repo / name).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)


def _make_repo(base, name, branch="master"):
    r = base / name
    r.mkdir()
    _git(r, "init", "-q", "-b", branch)
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "t")
    _commit(r, "main.go", GO, "init")
    return r


@pytest.fixture
def repo(tmp_path):
    return _make_repo(tmp_path, "pos")


@pytest.fixture
def index(repo):
    return RepoIndex({"pos": str(repo)})


# ---------------------------------------------------------------------------
# 1. símbolo cerca de la cita
# ---------------------------------------------------------------------------


def test_symbols_for_citation_descarta_minusculas_sueltas():
    assert cit.symbols_for_citation("Ver `ResolveToken()` y `pkg.Func` y `max_retries` y `token` y `timeout`") == [
        "ResolveToken", "Func", "max_retries",
    ]
    assert cit.symbols_for_citation("sin backticks") == []


def test_estados_ok_lejano_ausente_y_minuscula(index):
    text = (
        "Ver `ResolveToken()` en pos/main.go:6.\n"
        "Ver `FarSymbol` en pos/main.go:6.\n"
        "Ver `Nothing_here()` en pos/main.go:6.\n"
        "Ver `token` en pos/main.go:6.\n"
        "Ver pos/gone.go:3 y pos/main.go:900.\n"
    )
    r = cit_check.verify(text, index)
    lines = r.text.split("\n")
    assert lines[0] == "Ver `ResolveToken()` en pos/main.go:6."
    assert lines[1] == "Ver `FarSymbol` en pos/main.go:6 ⚠ símbolo a 54 líneas."
    assert lines[2] == "Ver `Nothing_here()` en pos/main.go:6 ⚠ símbolo no está en el archivo."
    assert lines[3] == "Ver `token` en pos/main.go:6."  # minúscula suelta: no es símbolo
    assert "⚠ no existe" in lines[4] and "⚠ línea fuera de rango" in lines[4]
    # el contador histórico se conserva; los nuevos se suman aparte
    assert (r.total, r.missing) == (6, 2)
    assert (r.ok, r.symbol_far, r.symbol_absent) == (2, 1, 1)
    assert r.summary == "6 citas verificadas, 2 no encontradas · símbolo: ok 2, lejano 1, ausente 1"


def test_check_symbols_del_knowledge_no_cambia(index, repo):
    # knowledge check sigue usando todos los símbolos de la línea (sin el filtro de minúsculas)
    probe = cit.Citation(doc="d", md_line=1, raw="main.go:6", path="main.go", line=6)
    cit._check_symbols(probe, (repo / "main.go").read_text().splitlines(), "Ver `FarSymbol`")
    assert probe.status == cit.STATUS_SYMBOL_MOVED and probe.distance == 54 and "línea 60" in probe.note


class _FakeAgent:
    def __init__(self, name, reply):
        self.name, self.reply = name, reply
        self.last_usage = {"agent": name, "model": "claude-haiku-4-5", "requests": 1, "input_tokens": 10,
                           "output_tokens": 5, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
                           "cache_creation_5m": 0, "cache_creation_1h": 0}

    def run(self, task):
        return self.reply


@pytest.fixture
def orch(tmp_path, repo, monkeypatch):
    for key, value in (
        ("USE_BEDROCK", False), ("EVALUATOR_ENABLED", False), ("META_AGENT_ENABLED", False),
        ("OUTPUT_DIR", tmp_path / "out"), ("REPOS", {"pos": str(repo)}), ("SESSION_MAX_USD", 100.0),
        ("BURST_GAP_SECONDS", 0),
    ):
        monkeypatch.setattr(config, key, value, raising=False)
    o = Orchestrator()
    o.quiet = True
    o.route_medians_fn = lambda: {}
    state = SimpleNamespace(orch=o, reply="ok")
    monkeypatch.setattr(o, "_get_agent", lambda name: _FakeAgent(name, state.reply))
    return state


def test_contadores_de_simbolo_en_el_registro_y_en_stats(orch):
    # una línea con varios símbolos vale si alguno está cerca: por eso el lejano va en su propia línea
    orch.reply = "Está en `FarSymbol` (pos/main.go:6).\nTambién `ResolveToken()` (pos/main.go:6); falta pos/gone.go:1."
    orch.orch.run("¿dónde se resuelve el token?")
    rec = orch.orch.last_run_record
    assert (rec["citations_total"], rec["citations_missing"]) == (3, 1)
    assert (rec["citations_ok"], rec["citations_symbol_far"], rec["citations_symbol_absent"]) == (1, 1, 0)
    out = CliRunner().invoke(minka_main.app, ["stats"]).output
    assert "citas no encontradas: 1/3" in out and "símbolo ok 1, lejano 1, ausente 0" in out


def test_evals_quality_puntua_igual_con_marcas_de_simbolo(repo):
    repos = {"pos": str(repo)}
    index = RepoIndex(repos)
    casos = [
        (EvalCase(id="l", input="¿?", type="location", expect={"file": "main.go", "symbol": "ResolveToken"}),
         "Está en pos/main.go:6 (`ResolveToken()`).\nVer también `FarSymbol` en pos/main.go:6."),
        (EvalCase(id="c", input="¿?", type="count", expect={"grep": "FarSymbol", "n": 1}),
         "Hay 1 uso de `FarSymbol` (pos/main.go:6).\n`Nothing_here()` en pos/main.go:6."),
    ]
    for case, salida in casos:
        verified = cit_check.verify(salida, index).text
        assert "⚠ símbolo" in verified
        before, after = qe.score(case, salida, repos), qe.score(case, verified, repos)
        assert (after.verdict, after.note) == (before.verdict, before.note) and before.verdict == qe.VERDICT_OK
    assert cc.strip_marks("x pos/main.go:6 ⚠ símbolo a 54 líneas. y ⚠ símbolo no está en el archivo") == "x pos/main.go:6. y"


# ---------------------------------------------------------------------------
# 2. antigüedad del contexto (team/ solo como evidencia)
# ---------------------------------------------------------------------------


@pytest.fixture
def knowledge(tmp_path):
    k = tmp_path / "proj" / "knowledge"
    (k / "team").mkdir(parents=True)
    (k / "extracted").mkdir()
    (k / "team" / "session_log.md").write_text(
        "# Bitácora\n\n## 2026-11-10 — reciente\n- algo\n\n## 2026-10-01 — vieja\n- otra cosa\n"
    )
    (k / "team" / "decisions_log.md").write_text(
        "## ADR-001 — cifrado\n**Fecha:** 2026-09-01\n**Estado:** vigente\n\n## ADR-002 — reciente\n**Fecha:** 2026-11-12\n**Estado:** vigente\n"
    )
    return k


def test_fechas_del_contexto_dejan_de_ser_falso_positivo_y_marcan_antiguedad(knowledge, monkeypatch):
    monkeypatch.setattr(config, "CONTEXT_STALE_DAYS", 30, raising=False)
    dates = cc.load_context_dates(knowledge)
    assert dates == {"2026-11-10": "session_log", "2026-10-01": "session_log", "2026-09-01": "decisions_log", "2026-11-12": "decisions_log"}
    text = "Se decidió el 2026-10-01 y se retomó el 2026-11-10; nada el 2026-01-01."
    r = cc.verify(text, RepoIndex({}), previous=[], today=TODAY, context_dates=dates)
    assert "(entrada del 2026-10-01; verifica en el código)" in r.text
    assert "(doc: session_log)" in r.text
    assert r.text.rstrip().endswith("⚠ sin respaldo: 2026-01-01")
    assert (r.context_dates, r.context_stale, r.numbers_unsupported) == (2, 1, 1)
    # sin el índice (comportamiento previo) las fechas del contexto salían "sin respaldo"
    r0 = cc.verify(text, RepoIndex({}), previous=[], today=TODAY)
    assert r0.numbers_unsupported == 3


def test_adr_con_fecha_vieja_pide_verificar(knowledge, monkeypatch):
    monkeypatch.setattr(config, "CONTEXT_STALE_DAYS", 30, raising=False)
    decisions = {"ADR-001": "2026-09-01", "ADR-002": "2026-11-12"}
    r = cc.verify("Según ADR-001 y ADR-002.", RepoIndex({}), previous=[], today=TODAY, decisions=decisions)
    assert r.text.endswith("(doc: decisions_log.md) (entrada del 2026-09-01; verifica en el código)")
    assert r.context_stale == 1 and r.decisions_unsupported == 0


def test_team_no_entra_en_el_knowledge_del_verificador_ni_del_prompt(knowledge, orch, monkeypatch):
    assert all(not path.startswith("team/") for path, _, _ in cc.load_project_docs(knowledge))
    monkeypatch.setattr(config, "PROJECTS_DIR", knowledge.parent.parent, raising=False)
    monkeypatch.setattr(config, "ACTIVE_PROJECT", "proj", raising=False)
    monkeypatch.setattr(config, "CONTEXT_STALE_DAYS", 30, raising=False)
    monkeypatch.setattr(cc, "date", SimpleNamespace(today=lambda: __import__("datetime").date(2026, 11, 16), fromisoformat=__import__("datetime").date.fromisoformat))
    orch.reply = "El cambio se registró el 2026-10-01 en la bitácora."
    out = orch.orch.run("¿cuándo se cambió la llave?")
    assert "(entrada del 2026-10-01; verifica en el código)" in out and "sin respaldo" not in out
    # lo que viaja al modelo no incluye el session_log: ni en el bloque vivo ni en el knowledge del agente
    assert "2026-10-01" not in (orch.orch._live_block() or "")


# ---------------------------------------------------------------------------
# 3. rama por repo
# ---------------------------------------------------------------------------


@pytest.fixture
def repos3(tmp_path):
    tmp_path = tmp_path / "r3"
    tmp_path.mkdir()
    pos = _make_repo(tmp_path, "pos")
    _git(pos, "checkout", "-q", "-b", "feature/x")
    _commit(pos, "a.go", "a\n", "a")
    _commit(pos, "b.go", "b\n", "b")  # +2 sobre master
    hsm = _make_repo(tmp_path, "hsm")
    _git(hsm, "checkout", "-q", "-b", "feature/y")
    _git(hsm, "checkout", "-q", "master")
    _commit(hsm, "m.go", "m\n", "m")  # master avanza: feature/y queda 1 detrás
    _git(hsm, "checkout", "-q", "feature/y")
    core = _make_repo(tmp_path, "core")  # en master
    main_only = _make_repo(tmp_path, "mainonly", branch="main")
    return {"pos": str(pos), "hsm": str(hsm), "core": str(core), "mainonly": str(main_only)}


def test_repo_branches_foto_y_etiquetas(repos3, tmp_path):
    snap = br.RepoBranches({**repos3, "roto": str(tmp_path / "no-repo")})
    assert snap.infos["pos"].branch == "feature/x" and snap.infos["pos"].ahead == 2 and snap.infos["pos"].behind == 0
    assert snap.infos["hsm"].behind == 1 and snap.infos["hsm"].ahead == 0
    assert snap.infos["core"].on_base and snap.infos["mainonly"].on_base and snap.infos["mainonly"].base == "main"
    assert snap.infos["roto"].branch is None and snap.infos["roto"].on_base
    assert [i.alias for i in snap.off_base()] == ["pos", "hsm"]
    assert snap.search_note() == "Búsqueda sobre: pos@feature/x (+2 sobre master), hsm@feature/y (1 detrás)"
    line = snap.status_line()
    assert "pos@feature/x (+2 sobre master)" in line and "core@master" in line and line.endswith(" ms")
    assert snap.elapsed_ms >= 0
    assert br.RepoBranches({"core": repos3["core"]}).search_note() is None
    assert br.RepoBranches({}).status_line() == "—"


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [("No existe un handler para eso.", True), ("no hay retry", True), ("[NO ENCONTRADO] nada", True),
     ("Existe en pos/x.go:1", False), ("pos/x.go:1 ⚠ no existe", True)],
)
def test_mentions_absence(texto, esperado):
    assert br.mentions_absence(texto) is esperado


def test_nota_de_busqueda_solo_en_ausencias_y_fuera_de_la_memoria(orch, repos3, monkeypatch):
    monkeypatch.setattr(config, "REPOS", repos3, raising=False)
    orch.reply = "No hay implementación del retry en los repos."
    out = orch.orch.run("¿dónde está el retry?")
    assert "\n\n_Búsqueda sobre: pos@feature/x (+2 sobre master), hsm@feature/y (1 detrás)_" in out  # antes de la nota "acotada"
    assert "Búsqueda sobre" not in orch.orch.memory.turns[-1].result
    orch.reply = "Está en `ResolveToken()` (pos/main.go:6)."
    out = orch.orch.run("¿dónde se resuelve el token?")
    assert "Búsqueda sobre" not in out
    # en master en todos los repos: ninguna nota aunque niegue
    monkeypatch.setattr(config, "REPOS", {"core": repos3["core"]}, raising=False)
    orch.reply = "No existe ese módulo."
    assert "Búsqueda sobre" not in orch.orch.run("¿existe el módulo X?")


def test_nota_tambien_en_agente_forzado(orch, repos3, monkeypatch):
    monkeypatch.setattr(config, "REPOS", repos3, raising=False)
    orch.reply = "[NO ENCONTRADO] no hay rastro de la función."
    out = orch.orch.run("¿dónde está la función Z?", forced_agent="architecture")
    assert "Búsqueda sobre: pos@feature/x" in out


def test_status_y_resumen_muestran_las_ramas(orch, repos3, monkeypatch):
    monkeypatch.setattr(config, "REPOS", repos3, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-de-prueba")
    monkeypatch.setattr(minka_main, "_make_orchestrator", lambda: orch.orch)
    monkeypatch.setattr(minka_main, "_is_tty", lambda: False)
    result = CliRunner().invoke(minka_main.app, [], input="/status\nsalir\n")
    assert result.exit_code == 0 and "Ramas" in result.output and "pos@feature/x" in result.output
    orch.reply = "No hay nada."
    orch.orch.run("¿hay algo?")
    with minka_main.stderr_console.capture() as cap:
        minka_main._print_run_summary(orch.orch, quiet=False)
    assert "ramas: pos@feature/x (+2 sobre master)" in cap.get()
