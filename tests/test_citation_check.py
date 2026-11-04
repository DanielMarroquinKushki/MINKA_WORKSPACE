"""Citas de la respuesta verificadas contra REPOS (core/orchestrator/citation_check.py) y su
gancho en Orchestrator._record_turn. Repos temporales, sin red ni modelo."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from minka import main as minka_main
from minka.config_loader import config
from minka.core import metrics
from minka.core.orchestrator import citation_check as cc
from minka.core.orchestrator import planner
from minka.core.orchestrator.orchestrator import Orchestrator
from minka.core.tools.analysis.citations import RepoIndex


@pytest.fixture
def repos(tmp_path: Path) -> dict[str, str]:
    pagos = tmp_path / "real" / "svc-pagos"
    (pagos / "service").mkdir(parents=True)
    (pagos / "service" / "token.go").write_text("package service\n" * 50, encoding="utf-8")
    (pagos / "service" / "main.go").write_text("package main\n" * 10, encoding="utf-8")
    ledger = tmp_path / "real" / "svc-ledger"
    (ledger / "service").mkdir(parents=True)
    (ledger / "service" / "main.go").write_text("package main\n" * 10, encoding="utf-8")
    links = tmp_path / "links"
    links.mkdir()
    (links / "svc-pagos").symlink_to(pagos)
    return {"pagos": str(links / "svc-pagos"), "ledger": str(ledger)}


@pytest.fixture
def index(repos) -> RepoIndex:
    return RepoIndex(repos)


def test_anota_inexistente_y_fuera_de_rango(index):
    text = "Ver service/token.go:42 y service/gone.go:3; también service/token.go:900."
    r = cc.verify(text, index)
    assert r.text == (
        "Ver service/token.go:42 y service/gone.go:3 ⚠ no existe; "
        "también service/token.go:900 ⚠ línea fuera de rango."
    )
    assert (r.total, r.missing) == (3, 2)
    assert r.summary == "3 citas verificadas, 2 no encontradas"


def test_marcador_fuera_del_backtick(index):
    r = cc.verify("La cita `service/gone.go:3` falla.", index)
    assert r.text == "La cita `service/gone.go:3` ⚠ no existe falla."


def test_alias_basename_ambigua_y_elidida_no_se_marcan(index):
    text = "PAGOS/service/token.go:1, token.go:2, service/main.go:1 y svc-pagos/.../token.go:3"
    r = cc.verify(text, index)
    assert r.text == text
    assert (r.total, r.missing) == (4, 0)


def test_rechaza_rutas_fuera_de_repos(index):
    r = cc.verify("Mira ../fuera.go:1", index)
    assert r.text.endswith("../fuera.go:1 ⚠ no existe") and r.missing == 1


def test_ignora_bloques_de_codigo_y_comillas(index):
    text = "\n".join(
        [
            "Ejemplo:",
            "```",
            "ruta/inventada.go:10",
            "```",
            'y el literal "config/app.yml:1" tampoco cuenta, pero service/gone.go:5 sí.',
        ]
    )
    r = cc.verify(text, index)
    assert "ruta/inventada.go:10" in r.text and "inventada.go:10 ⚠" not in r.text
    assert '"config/app.yml:1"' in r.text and "app.yml:1 ⚠" not in r.text
    assert r.text.endswith("service/gone.go:5 ⚠ no existe sí.")
    assert (r.total, r.missing) == (1, 1)


def test_sin_citas_o_sin_repos_devuelve_el_mismo_texto(index):
    assert cc.verify("sin citas", index).text == "sin citas"
    assert cc.verify("", index) == cc.CheckResult("", 0, 0)
    assert cc.verify("service/gone.go:1", None).text == "service/gone.go:1"
    assert cc.verify("service/gone.go:1", RepoIndex({})).missing == 0


def test_cita_repetida_se_anota_cada_vez(index):
    r = cc.verify("service/gone.go:1 y otra vez service/gone.go:1", index)
    assert r.text.count("⚠ no existe") == 2 and (r.total, r.missing) == (2, 2)


def test_no_duplica_marcador_si_ya_estaba(index):
    r = cc.verify("service/gone.go:1 ⚠ no existe", index)
    assert r.text.count("⚠ no existe") == 1


# ---------------------------------------------------------------------------
# orquestador: gancho, memoria, registro y línea final
# ---------------------------------------------------------------------------


class _FakeAgent:
    def __init__(self, name, reply):
        self.name = name
        self.reply = reply
        self.last_usage = {
            "agent": name, "model": "claude-haiku-4-5", "requests": 1, "input_tokens": 10,
            "output_tokens": 5, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
            "cache_creation_5m": 0, "cache_creation_1h": 0,
        }

    def run(self, task):
        self.conversation_seen = list(getattr(self, "conversation", []))
        return self.reply


REPLY = "El token vive en service/token.go:42 y antes en service/gone.go:3."


@pytest.fixture
def orch(repos, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "USE_BEDROCK", False)
    monkeypatch.setattr(config, "EVALUATOR_ENABLED", False)
    monkeypatch.setattr(config, "META_AGENT_ENABLED", False)
    monkeypatch.setattr(config, "LIVE_CONTEXT_ENABLED", False, raising=False)
    monkeypatch.setattr(config, "CITATION_CHECK_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path / "out")
    monkeypatch.setattr(config, "REPOS", repos, raising=False)
    monkeypatch.setattr(planner, "_llm_plan", lambda *a, **k: pytest.fail("el planner no debía ejecutarse"))
    o = Orchestrator()
    o.quiet = True
    agents: dict[str, _FakeAgent] = {}
    monkeypatch.setattr(o, "_get_agent", lambda name: agents.setdefault(name, _FakeAgent(name, REPLY)))
    o.fake_agents = agents
    return o


def test_respuesta_anotada_memoria_y_registro(orch):
    out = orch.run("¿dónde se resuelve el token?")
    assert "service/gone.go:3 ⚠ no existe" in out and "service/token.go:42 ⚠" not in out
    assert "⚠ no existe" in orch.memory.turns[-1].result
    assert (orch.last_citations_total, orch.last_citations_missing) == (2, 1)
    assert orch.last_run_record["citations_total"] == 2
    assert orch.last_run_record["citations_missing"] == 1
    # El siguiente turno hereda la cita ya marcada.
    orch.run("¿dónde se valida el token?")
    hist = orch.fake_agents["architecture_agent"].conversation_seen
    assert any("⚠ no existe" in m["content"] for m in hist if m["role"] == "assistant")


def test_interruptor_apagado_deja_el_texto_intacto(orch, monkeypatch):
    monkeypatch.setattr(config, "CITATION_CHECK_ENABLED", False, raising=False)
    out = orch.run("¿dónde se resuelve el token?")
    assert "⚠" not in out and "⚠" not in orch.memory.turns[-1].result
    assert (orch.last_citations_total, orch.last_citations_missing) == (0, 0)


def test_agente_forzado_tambien_verifica(orch):
    out = orch.run("¿dónde se resuelve el token?", forced_agent="architecture")
    assert "service/gone.go:3 ⚠ no existe" in out and orch.last_run_record["citations_missing"] == 1


def test_indice_se_rehace_si_cambia_repos(orch, monkeypatch, tmp_path):
    first = orch._citation_index()
    assert orch._citation_index() is first
    monkeypatch.setattr(config, "REPOS", {"otro": str(tmp_path)}, raising=False)
    assert orch._citation_index() is not first


def test_aggregate_y_stats_suman_citas():
    base = dict(run_id="r", source="cli", project="demo", cwd="/", router="info_direct", plan=["a"],
                fix_loop_iters=0, ok=True, duration_s=1.0, usage={"per_agent": [], "totals": {}})
    r1 = metrics.build_run_record(**base, citations_total=3, citations_missing=1)
    r2 = metrics.build_run_record(**base)
    agg = metrics.aggregate([r1, r2, r1])
    assert (agg["citations_total"], agg["citations_missing"]) == (6, 2)


def test_linea_final_de_citas_en_stderr(monkeypatch):
    lines = []
    monkeypatch.setattr(minka_main, "stderr_console", type("C", (), {"print": lambda self, t, **k: lines.append(t)})())
    monkeypatch.delenv("MINKA_QUIET", raising=False)
    fake = type("O", (), {})()
    fake.last_run_record = {"tokens": {}, "plan": ["a"], "citations_total": 4, "citations_missing": 1}
    minka_main._print_run_summary(fake, quiet=False)
    assert any("4 citas verificadas, 1 no encontradas" in ln for ln in lines)
    lines.clear()
    fake.last_run_record["citations_total"] = 0
    minka_main._print_run_summary(fake, quiet=False)
    assert not any("citas verificadas" in ln for ln in lines)
    lines.clear()
    minka_main._print_run_summary(fake, quiet=True)
    assert lines == []


def test_cli_stats_muestra_citas(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
    base = dict(run_id="r", source="cli", project="demo", cwd="/", router="info_direct", plan=["a"],
                fix_loop_iters=0, ok=True, duration_s=1.0, usage={"per_agent": [], "totals": {}})
    record = metrics.build_run_record(**base, citations_total=3, citations_missing=1)
    monkeypatch.setattr(metrics, "read_run_records", lambda *a, **kw: [record])
    result = CliRunner().invoke(minka_main.app, ["stats"])
    assert result.exit_code == 0, result.output
    assert "citas no encontradas" in result.output
