"""`/mal` en el REPL, routing_feedback.jsonl (0600/0700, sin texto largo) y
`minka evals routing --from-feedback` → borrador que info_cases carga. Sin red."""

from __future__ import annotations

import json
import os
import stat
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from minka import main as minka_main
from minka.config_loader import config
from minka.core.evaluator import routing_evals
from minka.core.evaluator import routing_feedback as rf
from minka.core.orchestrator.orchestrator import Orchestrator

ARCH = "architecture_agent"
RECORD = {"run_id": "abc123", "router": "llm", "reason": "plan de 2 pasos", "confidence": "media",
          "plan": [ARCH, "report_agent"]}


def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def test_linea_con_permisos_0600_y_sin_texto_largo(demo_global):
    cfg = config
    path = rf.write_feedback(cfg, RECORD, "¿Dónde se valida el TOKEN?", "architecture", "x" * 200)
    assert path.name == rf.FEEDBACK_NAME and _mode(path) == 0o600 and _mode(path.parent) == 0o700
    (entry,) = rf.read_feedback(path)
    assert set(entry) == {"ts", "run_id", "project", "router", "reason", "confidence", "plan",
                          "request_norm", "request_sha", "expected", "note"}
    assert entry["run_id"] == "abc123" and entry["project"] == "demo" and entry["expected"] == ARCH
    assert entry["request_norm"] == "donde se valida el token" and entry["request_sha"] is None
    assert len(entry["note"]) == 80
    # pedido largo: solo hash, nunca el texto
    long_request = "¿dónde " + "se configura el timeout del cliente " * 6 + "?"
    rf.write_feedback(cfg, RECORD, long_request, None, "")
    second = rf.read_feedback(path)[1]
    assert second["request_norm"] is None and len(second["request_sha"]) == 16 and second["expected"] is None
    raw = path.read_text()
    assert "timeout del cliente" not in raw and "resultado" not in raw and _mode(path) == 0o600


def test_parse_command_y_normalize_agent():
    assert rf.parse_command("architecture debía ir directo") == (ARCH, "debía ir directo")
    assert rf.parse_command("pr_review_agent") == ("pr_review_agent", "")
    assert rf.parse_command("no era un reporte") == (None, "no era un reporte")
    assert rf.parse_command("") == (None, "")
    assert rf.normalize_agent("inventado") is None


def test_from_feedback_produce_casos_que_info_cases_carga(demo_global):
    rf.write_feedback(config, RECORD, "¿dónde se valida el token?", "architecture", "nota")
    rf.write_feedback(config, RECORD, "x " * 100, "report", "largo")  # solo hash: se omite
    path, n, skipped = rf.draft_cases(config)
    assert path.name == rf.DRAFT_NAME and (n, skipped) == (1, 1) and _mode(path) == 0o600
    draft = json.loads(path.read_text())
    assert draft["cases"][0]["expect"]["agents"] == [ARCH] and draft["cases"][0]["eval"] == "info"
    assert "BORRADOR" in draft["cases"][0]["_nota"]
    cases = routing_evals.info_cases(project_path=path)
    ids = [c["id"] for c in cases]
    assert "feedback_1" in ids and all(c.get("eval") == "info" for c in cases)
    assert routing_evals.info_cases(project_path=path.parent / "no-existe.json") == routing_evals.info_cases()


def test_cli_from_feedback_no_necesita_llm(demo_global, monkeypatch):
    monkeypatch.setattr(minka_main, "_require_llm_backend", lambda: pytest.fail("no debía pedir backend"))
    rf.write_feedback(config, RECORD, "¿dónde se valida el token?", "architecture", "nota")
    result = CliRunner().invoke(minka_main.app, ["--project", "demo", "evals", "routing", "--from-feedback"])
    assert result.exit_code == 0, result.output
    assert "Borrador" in result.output and "1 caso" in result.output and "no versionar" in result.output
    assert rf.draft_path(config).is_file()


class _FakeAgent:
    def __init__(self, name):
        self.name = name
        self.last_usage = {"agent": name, "model": "claude-haiku-4-5", "requests": 1, "input_tokens": 10,
                           "output_tokens": 5, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
                           "cache_creation_5m": 0, "cache_creation_1h": 0}

    def run(self, task):
        return "El token se valida en internal/auth/token.go:42."


@pytest.fixture
def repl(demo_global, tmp_path, monkeypatch):
    for key, value in (("USE_BEDROCK", False), ("EVALUATOR_ENABLED", False), ("META_AGENT_ENABLED", False),
                       ("OUTPUT_DIR", tmp_path / "out"), ("CITATION_CHECK_ENABLED", False), ("REPOS", {})):
        monkeypatch.setattr(config, key, value, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-de-prueba")
    o = Orchestrator()
    monkeypatch.setattr(o, "_get_agent", lambda name: _FakeAgent(name))
    monkeypatch.setattr(o, "_stderr", SimpleNamespace(print=lambda *a, **k: None))
    monkeypatch.setattr(minka_main, "_make_orchestrator", lambda: o)
    monkeypatch.setattr(minka_main, "_is_tty", lambda: False)
    return o


def test_repl_mal_registra_la_ultima_decision(repl):
    entrada = "/mal\n¿dónde se valida el token?\n/mal architecture debió ir directo\n/mal otra nota\nsalir\n"
    result = CliRunner().invoke(minka_main.app, [], input=entrada)
    assert result.exit_code == 0, result.output
    assert "No hay una corrida previa" in result.output and "Uso: /mal" in result.output
    assert result.output.count("Registrado: info_direct") == 2 and "no versionar" in result.output
    entries = rf.read_feedback(rf.feedback_path(config))
    assert [e["expected"] for e in entries] == [ARCH, None]
    assert entries[0]["note"] == "debió ir directo" and entries[1]["note"] == "otra nota"
    assert entries[0]["run_id"] == repl.last_run_record["run_id"] and entries[0]["plan"] == [ARCH]
    assert "token.go" not in rf.feedback_path(config).read_text()  # nunca la respuesta
