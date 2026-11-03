"""Registro por corrida (metrics/runs/<día>.jsonl): números y nombres, nunca tarea ni resultado."""

import json

import pytest

from minka._app import SOURCE_ENV
from minka.config_loader import config
from minka.core import metrics
from minka.core.orchestrator.orchestrator import Orchestrator
from minka.core.pricing import PRICING_VERSION, cost_usd

USAGE = {
    "model": "claude-haiku-4-5",
    "requests": 2,
    "input_tokens": 1000,
    "output_tokens": 100,
    "cache_creation_input_tokens": 500,
    "cache_read_input_tokens": 2500,
    "cache_creation_5m": 500,
    "cache_creation_1h": 0,
}


class _FakeAgent:
    def __init__(self, name, fail=False):
        self.name = name
        self.fail = fail
        self.last_usage = {"agent": name, **USAGE}

    def run(self, task):
        if self.fail:
            raise RuntimeError("boom")
        return f"resultado de {self.name}"


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "USE_BEDROCK", False)
    monkeypatch.setattr(config, "EVALUATOR_ENABLED", False)
    monkeypatch.setattr(config, "META_AGENT_ENABLED", False)
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(config, "ACTIVE_PROJECT", "demo", raising=False)
    monkeypatch.delenv(SOURCE_ENV, raising=False)
    monkeypatch.setattr(config, "PLANNER_SKIP_INFO", False, raising=False)  # el test mide el plan por keywords


def _orch(monkeypatch, fail=False):
    orch = Orchestrator()
    monkeypatch.setattr(orch, "_get_agent", lambda name: _FakeAgent(name, fail))
    return orch


def test_keyword_plan_registra_la_corrida(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    orch = _orch(monkeypatch)
    orch.run("analiza riesgos del repo secreto", use_llm_planner=False)

    files = list((tmp_path / "metrics" / "runs").glob("*.jsonl"))
    assert [f.name for f in files] == [f"{metrics.local_day()}.jsonl"]
    (record,) = metrics.read_run_records()
    assert record == orch.last_run_record
    assert record["router"] == "keyword"
    assert record["plan"] == ["architecture_agent"]
    assert record["source"] == "cli" and record["project"] == "demo" and record["ok"] is True
    assert record["tokens"]["input_tokens"] == 1000
    assert record["tokens"]["cache_read_input_tokens"] == 2500
    assert record["per_agent"][0]["agent"] == "architecture_agent"
    assert record["cost_usd"] == pytest.approx(cost_usd(USAGE))
    assert record["pricing_version"] == PRICING_VERSION
    assert record["run_id"] and record["duration_s"] >= 0 and record["cwd"]
    raw = json.dumps(record)
    assert "task" not in record and "result" not in record
    assert "secreto" not in raw and "resultado de" not in raw


def test_small_talk_se_registra_sin_tokens(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    _orch(monkeypatch).run("hola")
    (record,) = metrics.read_run_records()
    assert record["router"] == "small_talk" and record["plan"] == []
    assert record["cost_usd"] == 0.0 and record["tokens"]["requests"] == 0


def test_agente_forzado(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    _orch(monkeypatch).run("documenta", forced_agent="report")
    (record,) = metrics.read_run_records()
    assert record["router"] == "forced" and record["plan"] == ["report_agent"]


def test_source_del_entorno_pisa_el_atributo(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv(SOURCE_ENV, "bridge")
    orch = _orch(monkeypatch)
    orch.source = "repl"
    orch.run("analiza", use_llm_planner=False)
    assert metrics.read_run_records()[0]["source"] == "bridge"


def test_excepcion_del_agente_registra_ok_false(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    orch = _orch(monkeypatch, fail=True)
    with pytest.raises(RuntimeError):
        orch.run("analiza", use_llm_planner=False)
    (record,) = metrics.read_run_records()
    assert record["ok"] is False and record["router"] == "keyword"


def test_fallo_de_escritura_no_rompe_el_run(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    blocker = tmp_path / "no-es-directorio"
    blocker.write_text("x")
    monkeypatch.setattr(config, "OUTPUT_DIR", blocker)
    out = _orch(monkeypatch).run("analiza", use_llm_planner=False)
    assert "resultado de architecture_agent" in out
    assert metrics.write_run_record({"run_id": "x"}) is False


def test_cache_ratio():
    tokens = {"input_tokens": 1000, "cache_read_input_tokens": 3000, "cache_creation_input_tokens": 1000}
    assert metrics.cache_ratio(tokens) == pytest.approx(0.6)
    assert metrics.cache_ratio({"output_tokens": 50}) is None


def _record(**overrides):
    usage = {"per_agent": [{"agent": "architecture_agent", **USAGE}], "totals": USAGE}
    base = dict(
        run_id="r1",
        source="cli",
        project="demo",
        cwd="/tmp",
        router="llm",
        plan=["architecture_agent", "report_agent"],
        fix_loop_iters=0,
        ok=True,
        duration_s=42.4,
        usage=usage,
    )
    base.update(overrides)
    return metrics.build_run_record(**base)


def test_linea_final_sin_markup_y_con_alcance():
    line = metrics.format_run_summary(_record())
    assert line.startswith("4 100 tokens · cache 62% · $")
    assert " · 42 s · agentes: architecture→report " + metrics.SCOPE_NOTE in line
    assert "[" not in line  # apta para stderr sin markup de Rich
    assert "fix-loop ×3" in metrics.format_run_summary(_record(fix_loop_iters=3))


def test_json_out_mantiene_el_contrato_y_suma_claves(tmp_path):
    from minka.main import _write_json_out

    path = tmp_path / "out.json"
    record = _record()
    assert _write_json_out(path, "tarea", "report", "res", True, {"totals": USAGE}, record)
    payload = json.loads(path.read_text())
    # Lector antiguo (bridge): solo ok/result/usage.
    assert {k: payload[k] for k in ("ok", "result", "usage")} == {
        "ok": True,
        "result": "res",
        "usage": {"totals": USAGE},
    }
    assert payload["run_id"] == "r1" and payload["pricing_version"] == PRICING_VERSION
    assert payload["cost_usd"] == record["cost_usd"] and payload["duration_s"] == 42.4
    assert _write_json_out(path, "t", None, "r", False, None)  # sin record: claves en None
    assert json.loads(path.read_text())["run_id"] is None
