"""CLI: `minka` entra al REPL, `minka "tarea"` ejecuta, `minka stats` muestra métricas y
`minka "estadísticas de hoy"` sigue siendo una tarea. Sin LLM, red ni DATA_DIR real."""

import json

import pytest
from typer.testing import CliRunner

from minka import main as minka_main
from minka._app import QUIET_ENV
from minka.config_loader import config
from minka.core import metrics

USAGE_OPUS = {
    "agent": "architecture_agent",
    "model": "claude-opus-4-8",
    "requests": 3,
    "input_tokens": 10_000,
    "output_tokens": 500,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 30_000,
    "cache_creation_5m": 0,
    "cache_creation_1h": 0,
}
USAGE_HAIKU = {**USAGE_OPUS, "agent": "report_agent", "model": "claude-haiku-4-5", "requests": 1}


def _record(run_id, project, usages, **kw):
    totals = {k: sum(u[k] for u in usages) for k in metrics.TOKEN_KEYS}
    base = dict(
        run_id=run_id,
        source="cli",
        project=project,
        cwd="/tmp",
        router="llm",
        plan=[u["agent"] for u in usages],
        fix_loop_iters=0,
        ok=True,
        duration_s=30.0,
        usage={"per_agent": usages, "totals": totals},
    )
    base.update(kw)
    return metrics.build_run_record(**base)


class _FakeOrch:
    def __init__(self):
        self.runs: list[str] = []
        self.kwargs: list[dict] = []
        self.last_run_ok = True
        self.last_usage = {"per_agent": [USAGE_OPUS], "totals": USAGE_OPUS}
        self.last_run_record = _record("abc", "demo", [USAGE_OPUS])
        self.memory = []
        self.cleared = 0

    def clear_session(self):
        self.cleared += 1

    def run(self, task, **kwargs):
        self.runs.append(task)
        self.kwargs.append(kwargs)
        return "resultado fake"


@pytest.fixture
def cli(monkeypatch, tmp_path):
    fake = _FakeOrch()
    monkeypatch.setattr(minka_main, "_require_llm_backend", lambda: None)
    monkeypatch.setattr(minka_main, "_make_orchestrator", lambda: fake)
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
    monkeypatch.delenv(QUIET_ENV, raising=False)
    return fake


def _invoke(args, **kw):
    return CliRunner().invoke(minka_main.app, args, **kw)


def test_sin_argumentos_entra_al_repl(cli):
    result = _invoke([], input="salir\n")
    assert result.exit_code == 0, result.output
    assert "Sesión terminada" in result.output
    assert cli.runs == []


def test_repl_ejecuta_y_resume_cada_turno(cli):
    result = _invoke([], input="analiza X\nsalir\n")
    assert cli.runs == ["analiza X"]
    assert "resultado fake" in result.stdout
    assert metrics.SCOPE_NOTE in result.stderr and metrics.SCOPE_NOTE not in result.stdout


def test_tarea_ejecuta_con_linea_final_en_stderr(cli):
    result = _invoke(["analiza X", "--no-interactive"])
    assert result.exit_code == 0, result.output
    assert cli.runs == ["analiza X"]
    assert "resultado fake" in result.stdout
    assert "tokens · cache 75% · $" in result.stderr
    assert metrics.SCOPE_NOTE in result.stderr and metrics.SCOPE_NOTE not in result.stdout


def test_quiet_por_flag_y_por_entorno(cli, monkeypatch):
    assert "tokens ·" not in _invoke(["analiza X", "--no-interactive", "--quiet"]).stderr
    monkeypatch.setenv(QUIET_ENV, "1")
    assert "tokens ·" not in _invoke(["analiza X", "--no-interactive"]).stderr


def test_opciones_despues_de_la_tarea_como_las_manda_el_bridge(cli, tmp_path):
    out = tmp_path / "r.json"
    result = _invoke(["analiza X", "--agent", "report", "--json-out", str(out), "--no-interactive"])
    assert result.exit_code == 0, result.output
    assert cli.kwargs[0]["forced_agent"] == "report"
    payload = json.loads(out.read_text())
    assert payload["ok"] is True and payload["result"] == "resultado fake"
    assert payload["usage"]["totals"] == USAGE_OPUS and payload["run_id"] == "abc"


def test_estadisticas_de_hoy_es_una_tarea(cli):
    result = _invoke(["estadísticas de hoy", "--no-interactive"])
    assert result.exit_code == 0, result.output
    assert cli.runs == ["estadísticas de hoy"]
    assert "Sin corridas registradas" not in result.output


def test_stats_sin_datos_no_falla_ni_corre_nada(cli):
    result = _invoke(["stats"])
    assert result.exit_code == 0, result.output
    assert f"Sin corridas registradas para día {metrics.local_day()}" in result.output
    assert cli.runs == [] and "dev-agent" not in result.output  # sin banner ni corrida


def test_stats_con_datos_y_filtros(cli, tmp_path):
    runs_dir = tmp_path / "metrics" / "runs"
    runs_dir.mkdir(parents=True)
    records = [
        _record("r1", "demo", [USAGE_OPUS, USAGE_HAIKU]),
        _record("r2", "otro", [USAGE_OPUS], router="keyword", fix_loop_iters=3, ok=False, source="cache_after"),
    ]
    (runs_dir / "2026-01-05.jsonl").write_text("\n".join(json.dumps(r) for r in records) + "\n")

    day = _invoke(["stats", "--day", "2026-01-05"])
    assert day.exit_code == 0, day.output
    assert "corridas 2 · ok 1" in day.output and metrics.SCOPE_NOTE in day.output
    assert "architecture" in day.output and "report" in day.output
    assert "keyword 1 · llm 1" in day.output and "1 corrida(s), 3 iteraciones" in day.output

    assert "corridas 1" in _invoke(["stats", "--day", "2026-01-05", "--project", "demo"]).output
    agent = _invoke(["stats", "--day", "2026-01-05", "--agent", "report"]).output
    assert "report" in agent and "architecture" not in agent
    assert "corridas 2" in _invoke(["stats", "--month", "2026-01"]).output
    por_source = _invoke(["stats", "--day", "2026-01-05", "--source", "cache_after"]).output
    assert "corridas 1" in por_source and "source cache_after" in por_source
    assert "Sin corridas" in _invoke(["stats", "--day", "2026-01-05", "--source", "nadie"]).output
    assert "Sin corridas registradas para día 2026-01-06" in _invoke(["stats", "--day", "2026-01-06"]).output
    assert cli.runs == []


def test_repl_manda_el_pedido_crudo_y_clear_limpia_la_memoria(cli):
    result = _invoke([], input="analiza X\n/clear\n/status\nsalir\n")
    assert result.exit_code == 0, result.output
    assert cli.runs == ["analiza X"]  # sin bloque de historial antepuesto
    assert cli.cleared == 1 and "Historial limpio" in result.output
    assert "0 turnos en sesión" in result.output
