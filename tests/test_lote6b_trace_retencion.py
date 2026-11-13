"""Lote 6b: `minka trace` (apagada por defecto, JSONL 0600/0700, eventos route/turn/tool/end sin
contenido), rotación anunciada, retención de agent_runs.jsonl sin tocar métricas y tamaños en
`minka doctor`. Cliente LLM falso."""

from __future__ import annotations

import json
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from minka import main as minka_main
from minka.config_loader import config
from minka.core import doctor, metrics, retention, trace_log
from minka.core.agents import base_agent
from minka.core.agents.base_agent import BaseAgent
from minka.core.orchestrator import router
from minka.core.orchestrator.orchestrator import Orchestrator

# ---------------------------------------------------------------------------
# agente con cliente falso (misma receta que test_lote6_operacion)
# ---------------------------------------------------------------------------


class _Agent(BaseAgent):
    def get_system_prompt(self) -> str:
        return "sys"


class _FakeClient:
    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = 0
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.calls += 1
        return self.steps.pop(0)


def _usage():
    return SimpleNamespace(input_tokens=100, output_tokens=10, cache_read_input_tokens=40, cache_creation_input_tokens=0)


def _text(text):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason="end_turn", usage=_usage(), model="m")


def _tool_use(path="a.go"):
    blocks = [SimpleNamespace(type="text", text="voy a leer"),
              SimpleNamespace(type="tool_use", name="read_file", input={"path": path, "token": "SECRETO"}, id="t1")]
    return SimpleNamespace(content=blocks, stop_reason="tool_use", usage=_usage(), model="m")


@pytest.fixture
def entorno(monkeypatch, tmp_path):
    data = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", data, raising=False)
    monkeypatch.setattr(config, "OUTPUT_DIR", data / "output")
    monkeypatch.setattr(config, "QUIET", True, raising=False)
    monkeypatch.setattr(config, "PROGRESS_ENABLED", False, raising=False)
    monkeypatch.setattr(config, "TRACE_ENABLED", False, raising=False)
    monkeypatch.setattr(config, "TRACE_KEEP_DAYS", 7, raising=False)
    monkeypatch.setattr(base_agent.console, "print", lambda *a, **k: None)
    monkeypatch.setattr(base_agent.ToolRegistry, "get", staticmethod(lambda name: lambda **kw: "línea 1\nlínea 2\nCONTENIDO-PRIVADO"))
    monkeypatch.setattr(base_agent, "resolve_repo_paths", lambda name, kwargs, repos: (kwargs, None))

    def make(steps, trace=None):
        a = _Agent.__new__(_Agent)
        a.name = "architecture_agent"
        a.config = config
        a.client = _FakeClient(steps)
        a.tools = []
        a.conversation = []
        a.show_banner = False
        a.live_context_block = a.decisions_block = ""
        a.response_profile = a.voice = None
        a.iteration_count = 0
        a.max_iterations = 20
        a.last_usage = a._empty_usage()
        a.trace = trace
        monkeypatch.setattr(a, "_recall_memories", lambda task: "", raising=False)
        monkeypatch.setattr(a, "_system_param", lambda: "sys", raising=False)
        monkeypatch.setattr(a, "_remember", lambda t, r: None, raising=False)
        return a

    return SimpleNamespace(make=make, data=data)


# ---------------------------------------------------------------------------
# apagada por defecto
# ---------------------------------------------------------------------------


def test_apagada_no_escribe_nada(entorno):
    assert trace_log.open_run(config, "abc") is None
    a = entorno.make([_tool_use(), _text("Listo.")])
    assert a.run("analiza") == "Listo."
    assert not (entorno.data / "output" / "trace").exists()


def test_flag_trace_enciende_config(monkeypatch, tmp_path):
    monkeypatch.setattr(minka_main, "_require_llm_backend", lambda: None)
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(config, "TRACE_ENABLED", False, raising=False)
    seen = {}

    class _O:
        last_run_ok = True
        last_usage = {"per_agent": [], "totals": {}}
        last_run_record = None
        memory = []

        def run(self, task, **kw):
            seen["trace"] = config.TRACE_ENABLED
            return "ok"

    monkeypatch.setattr(minka_main, "_make_orchestrator", lambda: _O())
    res = CliRunner().invoke(minka_main.app, ["--no-interactive", "--trace", "analiza"])
    assert res.exit_code == 0 and seen["trace"] is True


# ---------------------------------------------------------------------------
# encendida: archivo, permisos, eventos y redacción
# ---------------------------------------------------------------------------


def test_eventos_turn_y_tool_sin_contenido(entorno, monkeypatch):
    monkeypatch.setattr(config, "TRACE_ENABLED", True, raising=False)
    tr = trace_log.open_run(config, "run1")
    assert tr is not None and tr.path.name == "run1.jsonl"
    assert stat.S_IMODE(tr.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(tr.path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(tr.path.parent.parent.stat().st_mode) == 0o700
    a = entorno.make([_tool_use(), _text("Respuesta final con a.go:1.")], trace=tr)
    a.run("analiza")
    events = trace_log.read_run(tr.path)
    kinds = [e["event"] for e in events]
    assert kinds == ["turn", "tool", "turn"]
    turn, tool, last = events
    assert turn["agent"] == "architecture_agent" and turn["iteration"] == 1 and turn["stop_reason"] == "tool_use"
    assert turn["input_tokens"] == 100 and turn["cache_read_input_tokens"] == 40 and turn["tool_calls"] == 1
    assert isinstance(turn["ms"], int)
    assert tool["name"] == "read_file" and tool["args"] == {"path": "a.go"}  # "token" no está en TOOL_LOG_ARGS
    assert tool["result_lines"] == 3 and tool["result_chars"] > 0 and tool["blocked"] is False
    assert last["stop_reason"] == "end_turn" and last["iteration"] == 2
    raw = tr.path.read_text(encoding="utf-8")
    assert "CONTENIDO-PRIVADO" not in raw and "SECRETO" not in raw and "Respuesta final" not in raw and "voy a leer" not in raw


def test_tool_bloqueada_se_marca(entorno, monkeypatch):
    monkeypatch.setattr(config, "TRACE_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "TOOL_REPEAT_LIMIT", 1, raising=False)
    monkeypatch.setattr(base_agent.stderr_console, "print", lambda *a, **k: None)
    tr = trace_log.open_run(config, "run2")
    a = entorno.make([_tool_use(), _tool_use(), _text("fin")], trace=tr)
    a.run("analiza")
    tools = [e for e in trace_log.read_run(tr.path) if e["event"] == "tool"]
    assert [t["blocked"] for t in tools] == [False, True] and tools[1]["is_error"] is True


def test_texto_libre_pasa_por_redact(entorno, monkeypatch):
    monkeypatch.setattr(config, "TRACE_ENABLED", True, raising=False)
    tr = trace_log.open_run(config, "run3")
    tr.event("route", router="llm", reason="planner dijo AKIAABCDEFGHIJKLMNOP", confidence="media")
    raw = tr.path.read_text(encoding="utf-8")
    assert "AKIAABCDEFGHIJKLMNOP" not in raw and "[REDACTED:" in raw
    with pytest.raises(ValueError):
        tr.event("otro")


def test_fallo_de_disco_no_rompe(entorno, monkeypatch):
    monkeypatch.setattr(config, "TRACE_ENABLED", True, raising=False)
    tr = trace_log.open_run(config, "run4")
    tr.path.unlink()
    tr.path.parent.chmod(0o500)
    try:
        tr.event("end", ok=True)  # no lanza
    finally:
        tr.path.parent.chmod(0o700)


# ---------------------------------------------------------------------------
# orquestador: route + end, rotación anunciada
# ---------------------------------------------------------------------------


class _FakeAgent:
    def __init__(self, name, reply="El token se resuelve en a.go:1."):
        self.name, self.reply = name, reply
        self.last_usage = {"agent": name, "model": "claude-haiku-4-5", "requests": 1, "input_tokens": 10, "output_tokens": 5,
                           "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_5m": 0,
                           "cache_creation_1h": 0}
        self.trace = "sin fijar"

    def run(self, task):
        return self.reply


@pytest.fixture
def orch(entorno, monkeypatch):
    monkeypatch.setattr(config, "USE_BEDROCK", False)
    monkeypatch.setattr(config, "EVALUATOR_ENABLED", False)
    monkeypatch.setattr(config, "META_AGENT_ENABLED", False)
    monkeypatch.setattr(config, "REPOS", {}, raising=False)
    monkeypatch.setattr(Orchestrator, "_agent_log_rotated", True)  # la rotación se prueba aparte
    o = Orchestrator()
    o.quiet = False
    lines = []
    monkeypatch.setattr(o, "_stderr", SimpleNamespace(print=lambda text, **kw: lines.append(str(text))))
    o.lines = lines
    agents = {}
    monkeypatch.setattr(o, "_get_agent", lambda name: agents.setdefault(name, _FakeAgent(name)))
    o.agents = agents
    return o


def test_route_y_end_con_reglas_descartadas(orch, monkeypatch):
    monkeypatch.setattr(config, "TRACE_ENABLED", True, raising=False)
    orch.run("¿dónde se resuelve el token?", use_llm_planner=False)
    assert orch.trace is not None and orch.agents["architecture_agent"].trace is orch.trace
    events = trace_log.read_run(orch.trace.path)
    assert [e["event"] for e in events] == ["route", "end"]
    route, end = events
    assert route["router"] == "info_direct" and route["confidence"] == "alta"
    assert route["evaluated"] == [
        "small_talk", "state", "meta", "review", "followup_change", "inherit_repo", "ambiguous",
    ]
    assert end["ok"] is True and end["interrupted"] == 0 and isinstance(end["duration_ms"], int)
    assert trace_log.list_runs(config)[-1] == orch.trace.path


def test_planner_registra_todas_las_locales_descartadas(orch, monkeypatch):
    monkeypatch.setattr(config, "TRACE_ENABLED", True, raising=False)
    from minka.core.orchestrator import planner

    steps = [{"agent": "architecture_agent", "task": "analiza"}, {"agent": "report_agent", "task": "reporta"}]
    monkeypatch.setattr(planner, "_llm_plan", lambda *a, **k: planner.LLMPlan(steps=steps))
    orch.run("analiza el flujo de pagos y genera un reporte", use_llm_planner=True)
    route = trace_log.read_run(orch.trace.path)[0]
    assert route["router"] == "llm" and route["evaluated"] == list(router.LOCAL_RULES)


def test_rotacion_anunciada_al_abrir(orch, monkeypatch):
    monkeypatch.setattr(config, "TRACE_ENABLED", True, raising=False)
    root = config.OUTPUT_DIR / "trace"
    old = root / (datetime.now() - timedelta(days=9)).strftime("%Y-%m-%d")
    recent = root / (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
    for d in (old, recent):
        d.mkdir(parents=True)
        (d / "x.jsonl").write_text("{}\n")
    orch.run("hola", use_llm_planner=False)
    assert not old.exists() and recent.exists()
    assert any("trace: 1 día(s) de trazas con más de 7 días retirados" in ln for ln in orch.lines)


def test_apagada_no_abre_traza_en_orquestador(orch):
    orch.run("hola", use_llm_planner=False)
    assert orch.trace is None and not (config.OUTPUT_DIR / "trace").exists()


# ---------------------------------------------------------------------------
# comando `minka trace`
# ---------------------------------------------------------------------------


def _invoke(args):
    return CliRunner().invoke(minka_main.app, args)


def test_comando_trace_lee_lista_y_limpia(entorno, monkeypatch):
    monkeypatch.setattr(config, "TRACE_ENABLED", True, raising=False)
    tr = trace_log.open_run(config, "abcdef0123456789")
    tr.event("route", router="info_direct", reason="consulta", confidence="alta", evaluated=["small_talk"], notices=[])
    tr.event("turn", agent="architecture_agent", iteration=1, input_tokens=1, output_tokens=2, cache_read_input_tokens=0,
             cache_creation_input_tokens=0, stop_reason="end_turn", tool_calls=0, ms=12)
    tr.event("end", ok=True, response_lines=3, interrupted=0, duration_ms=20)
    res = _invoke(["trace"])
    assert res.exit_code == 0 and "abcdef0123456789" in res.output
    res = _invoke(["trace", "abcdef"])
    assert res.exit_code == 0 and "route  info_direct" in res.output and "turn   architecture_agent #1" in res.output
    res = _invoke(["trace", "--last"])
    assert res.exit_code == 0 and "3 evento(s)" in res.output
    res = _invoke(["trace", "zzz"])
    assert res.exit_code == 1
    res = _invoke(["trace", "--clear"])
    assert res.exit_code == 0 and "1 traza(s)" in res.output and not (config.OUTPUT_DIR / "trace").exists()
    res = _invoke(["trace", "--last"])
    assert res.exit_code == 1 and "no hay trazas" in res.output


# ---------------------------------------------------------------------------
# retención de agent_runs.jsonl
# ---------------------------------------------------------------------------


def _entry(days_ago: int) -> str:
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    return json.dumps({"ts": ts, "agent": "architecture_agent", "iterations": 1, "task": "t", "result_preview": "r", "usage": {}}) + "\n"


def test_rotacion_agent_runs_con_copia_y_sin_tocar_metrics(entorno, monkeypatch):
    monkeypatch.setattr(config, "AGENT_LOG_KEEP_DAYS", 30, raising=False)
    log = retention.agent_log_path(config)
    log.parent.mkdir(parents=True)
    log.write_text(_entry(45) + _entry(31) + _entry(29) + _entry(0) + "basura sin ts\n", encoding="utf-8")
    runs = metrics.runs_dir(config)
    runs.mkdir(parents=True)
    (runs / "2026-01-01.jsonl").write_text("{}\n")
    before = (runs / "2026-01-01.jsonl").stat().st_mtime_ns
    removed = retention.rotate_agent_runs(config)
    assert removed == 2
    kept = log.read_text(encoding="utf-8").splitlines()
    assert len(kept) == 3 and kept[-1] == "basura sin ts"
    backup = log.with_name("agent_runs.jsonl.1")
    assert backup.is_file() and len(backup.read_text(encoding="utf-8").splitlines()) == 5
    assert (runs / "2026-01-01.jsonl").stat().st_mtime_ns == before
    assert retention.rotate_agent_runs(config) == 0  # idempotente
    assert "2 línea(s) con más de 30 días" in retention.rotation_notice(2, config)


def test_rotacion_sin_archivo_no_falla(entorno):
    assert retention.rotate_agent_runs(config) == 0


def test_orquestador_rota_una_vez_por_proceso_y_avisa(entorno, monkeypatch):
    monkeypatch.setattr(config, "USE_BEDROCK", False)
    monkeypatch.setattr(config, "EVALUATOR_ENABLED", False)
    monkeypatch.setattr(config, "META_AGENT_ENABLED", False)
    monkeypatch.setattr(config, "REPOS", {}, raising=False)
    monkeypatch.setattr(Orchestrator, "_agent_log_rotated", False)
    log = retention.agent_log_path(config)
    log.parent.mkdir(parents=True)
    log.write_text(_entry(40) + _entry(1), encoding="utf-8")
    o = Orchestrator()
    o.quiet = False
    lines = []
    monkeypatch.setattr(o, "_stderr", SimpleNamespace(print=lambda text, **kw: lines.append(str(text))))
    o.run("hola", use_llm_planner=False)
    assert any("agent_runs.jsonl: 1 línea(s)" in ln for ln in lines)
    log.write_text(_entry(40), encoding="utf-8")
    o.run("hola", use_llm_planner=False)
    assert len(log.read_text().splitlines()) == 1  # segunda corrida: no vuelve a rotar


# ---------------------------------------------------------------------------
# doctor: tamaños
# ---------------------------------------------------------------------------


def _cfg(tmp_path: Path, **over) -> SimpleNamespace:
    data = tmp_path / "data"
    (data / "projects" / "demo").mkdir(parents=True, exist_ok=True)
    (data / "active-context").mkdir(exist_ok=True)
    (data / "active-context" / "demo.md").write_text("- ticket", encoding="utf-8")
    (data / "config_local.py").write_text("", encoding="utf-8")
    (data / ".env").write_text("", encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    base = dict(DATA_DIR=data, OUTPUT_DIR=data / "output", PROJECTS_DIR=data / "projects", ACTIVE_CONTEXT_DIR=None,
                ACTIVE_PROJECT="demo", USE_BEDROCK=False, LLM_BACKEND="anthropic", AWS_PROFILE="perfil",
                REPOS={"pos": str(repo)}, RAG_ENABLED=False)
    base.update(over)
    return SimpleNamespace(**base)


def test_doctor_muestra_tamanos_y_avisa_por_umbral(tmp_path):
    cfg = _cfg(tmp_path, DOCTOR_SIZE_WARN_MB=1)
    (cfg.DATA_DIR / "output" / "trace" / "2026-01-01").mkdir(parents=True)
    (cfg.DATA_DIR / "output" / "trace" / "2026-01-01" / "x.jsonl").write_bytes(b"x" * 1_500_000)
    (cfg.DATA_DIR / "output" / "logs").mkdir(parents=True)
    (cfg.DATA_DIR / "output" / "logs" / "agent_runs.jsonl").write_bytes(b"y" * 1000)
    check = next(c for c in doctor.run_checks(cfg) if c.name == "Tamaño de datos")
    assert not check.ok and not check.critical
    assert "trace 1.5 MB" in check.detail and "logs 0.0 MB" in check.detail and "chromadb 0.0 MB" in check.detail
    assert "trace" in check.hint and "DOCTOR_SIZE_WARN_MB" in check.hint
    ok = next(c for c in doctor.run_checks(_cfg(tmp_path, DOCTOR_SIZE_WARN_MB=500)) if c.name == "Tamaño de datos")
    assert ok.ok
    assert not doctor.has_critical_failure([check])
