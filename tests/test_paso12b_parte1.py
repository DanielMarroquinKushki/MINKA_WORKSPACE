"""Paso 12b, parte 1 — medidor pasivo: calibración de símbolos por cita más cercana, permisos 0600/0700,
rotación de métricas que no toca el baseline, `stats --purge`, contadores nuevos, /bien y /mal sobre la
respuesta, `stats --freeze-baseline` y aviso de permisos en doctor. Sin red: repos git temporales."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from datetime import date
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from minka import main as minka_main
from minka.config_loader import config
from minka.core import doctor, metrics
from minka.core.evaluator import routing_feedback as rf
from minka.core.orchestrator import budget
from minka.core.orchestrator import citation_check as cit_check
from minka.core.orchestrator.orchestrator import Orchestrator
from minka.core.tools.analysis.citations import RepoIndex

ARCH = "architecture_agent"
FAKE_AWS_KEY = "AKIA" + "I" * 16  # construida en runtime para no dejar la cadena literal en el repo
GO_A = "package a\n\nfunc Alpha() {}\n" + "\n" * 20 + "func AlphaFar() {}\n"  # Alpha en 3, AlphaFar en 24
GO_B = "package b\n\n" + "\n" * 7 + "func Beta() {}\n"  # Beta en línea 10


def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "pos"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "t")
    (r / "a.go").write_text(GO_A)
    (r / "b.go").write_text(GO_B)
    _git(r, "add", ".")
    _git(r, "commit", "-q", "-m", "init")
    return r


# ---------------------------------------------------------------------------
# 1. calibración: símbolo → cita más cercana; rango a-b
# ---------------------------------------------------------------------------


def test_dos_citas_con_un_simbolo_cada_una_cero_marcas(repo):
    index = RepoIndex({"pos": str(repo)})
    r = cit_check.verify("Ver `Alpha()` (pos/a.go:3) y `Beta()` (pos/b.go:10).", index)
    assert "⚠" not in r.text and (r.ok, r.symbol_far, r.symbol_absent) == (2, 0, 0)
    # el mismo texto con el símbolo de la otra cita sí marca: la asignación es por cercanía, no por línea
    r2 = cit_check.verify("Ver `Beta()` (pos/a.go:3) y `Alpha()` (pos/b.go:10).", index)
    assert r2.symbol_absent == 2


def test_rango_a_b_usa_el_final_del_rango(repo):
    index = RepoIndex({"pos": str(repo)})
    r = cit_check.verify("La función `AlphaFar()` está en pos/a.go:3-24.", index)
    assert "⚠" not in r.text and r.ok == 1
    r2 = cit_check.verify("La función `AlphaFar()` está en pos/a.go:3.", index)
    assert "⚠ símbolo a 21 líneas" in r2.text and r2.symbol_far == 1


def test_caso_sender_68_simbolo_de_la_cita_vecina(repo):
    # Caso real de calibración: "sender.go:68 … `Proveedor` (otro.go:52-90)": el símbolo pertenece a la
    # segunda cita; la primera no debe marcarse como "símbolo a N líneas".
    index = RepoIndex({"pos": str(repo)})
    text = "El envío sale de pos/a.go:3 y usa el proveedor `Beta` (pos/b.go:8-12)."
    r = cit_check.verify(text, index)
    assert "⚠" not in r.text and r.ok == 2


# ---------------------------------------------------------------------------
# 2. permisos, rotación, purga
# ---------------------------------------------------------------------------


@pytest.fixture
def out_dir(tmp_path, monkeypatch):
    out = tmp_path / "out"
    monkeypatch.setattr(config, "OUTPUT_DIR", out)
    monkeypatch.setattr(config, "METRICS_KEEP_DAYS", 90, raising=False)
    return out


def _record(day: str, **extra):
    base = dict(run_id=f"r-{day}", ts=f"{day}T10:00:00+00:00", source="repl", project="demo", router="info_direct",
                plan=[ARCH], ok=True, cost_usd=0.1, tokens={"input_tokens": 100, "cache_read_input_tokens": 900,
                "cache_creation_input_tokens": 0, "output_tokens": 10}, citations_total=4, citations_missing=1)
    base.update(extra)
    return base


def _write_day(out, day, n=2, **extra):
    for i in range(n):
        metrics.append_private(out / "metrics" / "runs" / f"{day}.jsonl", json.dumps(_record(day, run_id=f"{day}-{i}", **extra)))


def test_registros_y_logs_con_0600_y_0700(out_dir):
    assert metrics.write_run_record(_record("2026-11-16"))
    runs = out_dir / "metrics" / "runs"
    assert _mode(out_dir / "metrics") == 0o700 and _mode(runs) == 0o700
    assert _mode(next(runs.glob("*.jsonl"))) == 0o600
    log = out_dir / "logs" / "agent_runs.jsonl"
    metrics.append_private(log, '{"ts": "2026-11-16T00:00:00+00:00"}')
    assert _mode(log.parent) == 0o700 and _mode(log) == 0o600
    # un archivo previo con permisos abiertos se cierra al volver a escribir
    os.chmod(log, 0o644)
    metrics.append_private(log, "{}")
    assert _mode(log) == 0o600


def test_rotacion_de_metricas_anunciada_y_sin_tocar_el_baseline(out_dir, monkeypatch):
    _write_day(out_dir, "2026-06-01", n=3)
    _write_day(out_dir, "2026-11-10", n=2)
    baseline = out_dir / "metrics" / "baseline_v3.json"
    metrics.write_private(baseline, "{}")
    old = os.utime(baseline, (0, 0))  # muy viejo: no debe importar
    files, lines = metrics.rotate_runs(config, today=date(2026, 11, 16))
    assert (files, lines) == (1, 3) and baseline.exists() and baseline.read_text() == "{}"
    assert sorted(p.name for p in (out_dir / "metrics" / "runs").glob("*.jsonl")) == ["2026-11-10.jsonl"]
    assert metrics.rotation_notice(files, lines, config) == "metrics/runs: 3 línea(s) en 1 archivo(s) con más de 90 días retiradas"
    assert metrics.rotate_runs(config, today=date(2026, 11, 16)) == (0, 0)
    del old
    # el orquestador lo anuncia una vez por proceso
    for key, value in (("USE_BEDROCK", False), ("EVALUATOR_ENABLED", False), ("META_AGENT_ENABLED", False), ("REPOS", {})):
        monkeypatch.setattr(config, key, value, raising=False)
    _write_day(out_dir, "2026-01-01", n=1)
    monkeypatch.setattr(Orchestrator, "_agent_log_rotated", False)
    lines_out = []
    o = Orchestrator()
    monkeypatch.setattr(o, "_stderr", SimpleNamespace(print=lambda text, **kw: lines_out.append(str(text))))
    o.run("hola")
    assert any("metrics/runs: 1 línea(s) en 1 archivo(s)" in ln for ln in lines_out)


def test_purge_simula_por_defecto_y_borra_con_yes(out_dir, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_DIR", out_dir / "projects", raising=False)
    _write_day(out_dir, "2026-10-01", n=2)
    _write_day(out_dir, "2026-11-15", n=1)
    metrics.append_private(out_dir / "logs" / "agent_runs.jsonl", "{}")
    metrics.append_private(out_dir / "projects" / "demo" / "evals" / "response_feedback.jsonl", "{}")
    baseline = out_dir / "metrics" / "baseline_v3.json"
    metrics.write_private(baseline, "{}")
    runner = CliRunner()
    res = runner.invoke(minka_main.app, ["stats", "--purge", "--que", "todo"])
    assert res.exit_code == 0 and "Simulación" in res.output and "metricas: 2 archivo(s), 3 línea(s)" in res.output
    assert "logs: 1 archivo(s)" in res.output and "feedback: 1 archivo(s)" in res.output
    assert len(list((out_dir / "metrics" / "runs").glob("*.jsonl"))) == 2  # nada borrado
    res = runner.invoke(minka_main.app, ["stats", "--purge", "--que", "todo", "--before", "2026-11-01", "--yes"])
    assert res.exit_code == 0 and "Borrados 1 archivo(s)" in res.output
    assert [p.name for p in (out_dir / "metrics" / "runs").glob("*.jsonl")] == ["2026-11-15.jsonl"]
    res = runner.invoke(minka_main.app, ["stats", "--purge", "--que", "todo", "--yes"])
    assert res.exit_code == 0 and "Borrados 3 archivo(s)" in res.output and baseline.exists()
    assert runner.invoke(minka_main.app, ["stats", "--purge", "--que", "todo", "--before", "ayer"]).exit_code == 2


def test_doctor_avisa_permisos_abiertos_sin_bloquear(out_dir, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_DIR", out_dir / "projects", raising=False)
    path = out_dir / "metrics" / "runs" / "2026-11-16.jsonl"
    metrics.append_private(path, "{}")
    assert doctor.loose_permissions(config) == []
    os.chmod(path, 0o644)
    assert doctor.loose_permissions(config) == ["metrics/runs/2026-11-16.jsonl"]
    check = [c for c in doctor.run_checks(config) if c.name == "Permisos de datos"][0]
    assert check.ok is False and check.critical is False and "1 archivo(s)" in check.detail
    assert doctor.has_critical_failure([check]) is False


# ---------------------------------------------------------------------------
# 3. contadores nuevos en el registro
# ---------------------------------------------------------------------------


class _FakeAgent:
    def __init__(self, name, reply="Está en `Alpha()` (pos/a.go:3)."):
        self.name, self.reply = name, reply
        self.last_usage = {"agent": name, "model": "claude-haiku-4-5", "requests": 1, "input_tokens": 10,
                           "output_tokens": 5, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
                           "cache_creation_5m": 0, "cache_creation_1h": 0}

    def run(self, task):
        return self.reply


@pytest.fixture
def orch(out_dir, monkeypatch):
    for key, value in (("USE_BEDROCK", False), ("EVALUATOR_ENABLED", False), ("META_AGENT_ENABLED", False),
                       ("REPOS", {}), ("SESSION_MAX_USD", 100.0), ("BURST_GAP_SECONDS", 0)):
        monkeypatch.setattr(config, key, value, raising=False)
    o = Orchestrator()
    o.quiet = True
    o.route_medians_fn = lambda: {}
    monkeypatch.setattr(o, "_get_agent", lambda name: _FakeAgent(name))
    return o


def test_contadores_nuevos_en_el_registro(orch):
    orch.run("¿dónde se resuelve el token?")
    rec = orch.last_run_record
    assert {k: rec[k] for k in ("detalle_views", "branches_off_base", "budget_gate", "estimate_shown", "profile")} == {
        "detalle_views": 0, "branches_off_base": 0, "budget_gate": None, "estimate_shown": False, "profile": "breve",
    }
    orch.investigations.append({"turn": 1, "request": "x", "agents": [ARCH], "text": "t", "line": "l"})
    assert orch.investigation() is not None and orch.investigation(1) is not None and orch.investigation(9) is None
    orch.run("¿dónde se firma el request?")
    assert orch.last_run_record["detalle_views"] == 2
    orch.run("¿dónde se valida?")
    assert orch.last_run_record["detalle_views"] == 0  # consumido por el registro anterior


def test_budget_gate_queda_en_el_registro(orch, monkeypatch):
    monkeypatch.setattr(config, "SESSION_MAX_USD", 0.01, raising=False)
    orch.budget = budget.BudgetState()
    orch.budget.session_usd = 5.0
    with pytest.raises(budget.BudgetGate):
        orch.run("¿dónde se resuelve el token?")
    assert orch.last_run_record["budget_gate"] == "sesion" and orch.last_run_record["ok"] is False


# ---------------------------------------------------------------------------
# 4. /bien y /mal sobre la respuesta
# ---------------------------------------------------------------------------


@pytest.fixture
def repl(demo_global, orch, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-de-prueba")
    monkeypatch.setattr(minka_main, "_make_orchestrator", lambda: orch)
    monkeypatch.setattr(minka_main, "_is_tty", lambda: False)
    return orch


def test_bien_y_mal_registran_sin_texto_y_con_la_nota_redactada(repl):
    pedido = "¿dónde se resuelve el token SECRETO-DEL-PEDIDO?"
    entrada = (
        f"/bien\n{pedido}\n/bien muy claro, llave {FAKE_AWS_KEY} incluida " + "x" * 90 + "\n"
        "/mal architecture debió ir directo\n/status\nsalir\n"
    )
    result = CliRunner().invoke(minka_main.app, [], input=entrada)
    assert result.exit_code == 0, result.output
    flat = " ".join(result.output.split())  # Rich corta las líneas largas a 80 columnas
    assert "No hay una corrida previa" in flat and "Uso: /bien [nota" in flat
    assert flat.count("Guardado en response_feedback.jsonl (0600)") == 2
    assert "Registrado: info_direct → esperado architecture" in flat  # /mal sigue en routing_feedback
    assert "Feedback : 1 /bien · 1 /mal" in flat and "/bien, /mal, salir" in flat
    path = rf.response_feedback_path(config)
    assert _mode(path) == 0o600 and _mode(path.parent) == 0o700
    entries = rf.read_feedback(path)
    assert [e["verdict"] for e in entries] == ["bien", "mal"]
    bien = entries[0]
    assert set(bien) == {"ts", "run_id", "project", "router", "plan", "verdict", "note", "note_len"}
    assert bien["run_id"] == repl.last_run_record["run_id"] and bien["router"] == "info_direct" and bien["plan"] == [ARCH]
    assert "[REDACTED:AWS_ACCESS_KEY]" in bien["note"] and FAKE_AWS_KEY not in bien["note"]
    assert len(bien["note"]) == 80 == bien["note_len"]
    raw = path.read_text()
    assert "SECRETO-DEL-PEDIDO" not in raw and "Alpha" not in raw and FAKE_AWS_KEY not in raw  # ni pedido ni respuesta
    assert rf.read_feedback(rf.feedback_path(config))[0]["expected"] == ARCH


# ---------------------------------------------------------------------------
# 5. baseline v3 congelado por comando explícito
# ---------------------------------------------------------------------------


def test_freeze_baseline_explicito_y_sin_sobrescribir(out_dir):
    _write_day(out_dir, "2026-11-06", n=3)
    _write_day(out_dir, "2026-11-10", n=2, router="llm", plan=[ARCH, "report_agent"], cost_usd=0.4)
    _write_day(out_dir, "2026-11-10", n=1, source="eval_quality")  # excluida
    _write_day(out_dir, "2026-11-13", n=5)  # fuera del rango
    runner = CliRunner()
    res = runner.invoke(minka_main.app, ["stats", "--freeze-baseline"])
    assert res.exit_code == 0, res.output
    path = metrics.baseline_path(config)
    assert _mode(path) == 0o600
    data = json.loads(path.read_text())
    assert data["range"] == ["2026-11-05", "2026-11-11"] and "no una meta" in data["note"] and "cambió de significado" in data["note"]
    cats = data["categories"]
    assert cats["corridas"]["n"] == 5 and cats["costo_mediano_info_direct/1"] == {"value": 0.1, "n": 3}
    assert cats["costo_mediano_llm/2+"] == {"value": 0.4, "n": 2} and cats["citas_no_encontradas_pct"] == {"value": 0.25, "n": 20}
    assert cats["cache_mediana"]["n"] == 5 and "n=" in res.output
    res = runner.invoke(minka_main.app, ["stats", "--freeze-baseline"])
    assert res.exit_code == 1 and "--force" in res.output
    assert runner.invoke(minka_main.app, ["stats", "--freeze-baseline", "--force"]).exit_code == 0
    # la rotación nunca lo toca aunque esté fuera de fecha
    metrics.rotate_runs(config, today=date(2030, 1, 1))
    assert path.exists() and not list((out_dir / "metrics" / "runs").glob("*.jsonl"))
