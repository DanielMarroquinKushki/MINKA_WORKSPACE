"""Línea base (core/baseline.py): prompt_hash de prompts + tools + modelos, en el registro de
métricas y en `minka stats`; la plantilla del orchestrator ya no induce `steps: []`."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from minka import main as minka_main
from minka.config_loader import config
from minka.core import baseline, metrics
from minka.core.tools.registry import ToolRegistry

TEMPLATE_PROMPT = Path(config.TEMPLATE_DIR) / "prompts" / "orchestrator_prompt.py"


def test_plantilla_sin_steps_vacios_inducidos():
    text = TEMPLATE_PROMPT.read_text(encoding="utf-8")
    assert '"steps": []' not in text
    assert "no puede ser JSON válido" not in text
    assert '"steps": [' in text  # el contrato de salida sigue


def test_prompt_hash_estable_y_cacheado(monkeypatch):
    baseline.reset_cache()
    monkeypatch.setattr(config, "ACTIVE_PROJECT", None, raising=False)
    h1 = baseline.prompt_hash()
    assert len(h1) == baseline.HASH_CHARS and h1 == baseline.compute_prompt_hash()
    calls = []
    monkeypatch.setattr(baseline, "compute_prompt_hash", lambda cfg=None: calls.append(1) or "x")
    assert baseline.prompt_hash() == h1 and calls == []  # cacheado por proyecto
    baseline.reset_cache()


def test_prompt_hash_cambia_con_prompt_tools_o_modelos(monkeypatch):
    base = baseline.compute_prompt_hash()
    monkeypatch.setattr(baseline, "load_system_prompt", lambda name: f"otro prompt {name}")
    assert baseline.compute_prompt_hash() != base
    monkeypatch.undo()
    monkeypatch.setattr(ToolRegistry, "get_tools_for_agent", staticmethod(lambda name: []))
    assert baseline.compute_prompt_hash() != base
    monkeypatch.undo()
    monkeypatch.setattr(config, "MODELS", {**config.MODELS, "planner": "otro-modelo"}, raising=False)
    assert baseline.compute_prompt_hash() != base


def test_prompt_hash_no_depende_del_knowledge(monkeypatch):
    base = baseline.compute_prompt_hash()
    from minka.core.knowledge import loader

    monkeypatch.setattr(loader.KnowledgeLoader, "load_for_agent", staticmethod(lambda n: None))
    assert baseline.compute_prompt_hash() == base


def test_registro_y_stats_llevan_prompt_hash(monkeypatch, tmp_path):
    base = dict(run_id="r", source="cli", project="demo", cwd="/", router="info_direct", plan=["a"],
                fix_loop_iters=0, ok=True, duration_s=1.0, usage={"per_agent": [], "totals": {}})
    r1 = metrics.build_run_record(**base, prompt_hash="abc123def456")
    r2 = metrics.build_run_record(**base, prompt_hash="ffff00001111")
    assert r1["prompt_hash"] == "abc123def456" and metrics.build_run_record(**base)["prompt_hash"] is None
    agg = metrics.aggregate([r1, r1, r2])
    assert agg["prompt_hashes"] == {"abc123def456": 2, "ffff00001111": 1}

    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(metrics, "read_run_records", lambda *a, **kw: [r1, r1, r2])
    result = CliRunner().invoke(minka_main.app, ["stats"])
    assert result.exit_code == 0, result.output
    assert "prompt_hash" in result.output and "abc123def456 ×2" in result.output
    assert "más de una línea base" in result.output
