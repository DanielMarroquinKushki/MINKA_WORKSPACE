"""Paso 12b, parte 2 — ausencias contadas contra REPOS (git grep, ≤ 3 nombres, ≤ 1 s), `stats --salud`,
chequeos de salud no bloqueantes en `doctor` y `stats --purge --que`. Sin red: repos git temporales,
agentes falsos, fechas fijas. Sin claves de ejemplo (el escaneo de secretos del CI no necesita excepción)."""

from __future__ import annotations

import json
import subprocess
from datetime import date, datetime, timezone

import pytest
from typer.testing import CliRunner

from minka import main as minka_main
from minka.config_loader import config
from minka.core import doctor, health, metrics
from minka.core.orchestrator import absence_check as ac
from minka.core.orchestrator.orchestrator import Orchestrator

ARCH = "architecture_agent"


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _make_repo(base, name, files: dict[str, str]):
    r = base / name
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "t")
    for rel, text in files.items():
        (r / rel).write_text(text)
    _git(r, "add", ".")
    _git(r, "commit", "-q", "-m", "init")
    return r


@pytest.fixture
def repos(tmp_path):
    pos = _make_repo(tmp_path, "svc-pos", {"a.go": "package a\n\nfunc FooBar_x() {}\n"})
    core = _make_repo(tmp_path, "svc-core", {"c.go": "package c\n\nfunc OnlyInCore() {}\n"})
    return {"pos": str(pos), "core": str(core)}


# ---------------------------------------------------------------------------
# 1. ausencias: refutada / no evaluable / confirmada / topes
# ---------------------------------------------------------------------------


def test_ausencia_sembrada_queda_refutada(repos):
    r = ac.check("Revisé el repo: no existe `FooBar_x` en el código.", repos)
    assert (r.said, r.refuted, r.unclear, r.confirmed) == (1, 1, 0, 0)
    assert r.items[0].status == ac.STATUS_REFUTED and r.items[0].found_in == "pos"
    assert r.as_fields() == {"absences_said": 1, "absences_refuted": 1, "absences_unclear": 0}


def test_sin_backticks_es_no_evaluable(repos):
    r = ac.check("No existe FooBar_x en el código y no hay un handler para eso.", repos)
    assert (r.said, r.refuted, r.unclear) == (2, 0, 2) and r.items == []


def test_articulo_y_no_encontrado_tambien_cuentan(repos):
    r = ac.check("no hay un `FooBar_x`. Además [NO ENCONTRADO: OnlyInCore].", repos)
    assert (r.said, r.refuted, r.unclear) == (2, 2, 0)
    assert [i.found_in for i in r.items] == ["pos", "core"]


def test_nombre_solo_en_otro_repo_queda_confirmada(repos):
    r = ac.check("no existe `OnlyInCore` en el pos.", repos)
    assert (r.said, r.refuted, r.unclear, r.confirmed) == (1, 0, 0, 1)
    assert r.items[0].repos == ["pos"] and r.items[0].status == ac.STATUS_CONFIRMED
    # sin repo mencionado se busca en todos: ahí sí existe
    assert ac.check("no existe `OnlyInCore` en el servicio.", repos).refuted == 1
    # la carpeta del repo también cuenta como mención
    assert ac.check("no existe `OnlyInCore` en svc-pos.", repos).confirmed == 1


def test_dentro_de_bloques_de_codigo_no_cuenta(repos):
    r = ac.check("```\nno existe `FooBar_x`\n```\nTexto normal.", repos)
    assert r.said == 0


def test_tope_de_tres_nombres(repos):
    text = " ".join(f"no existe `Nombre{i}`." for i in range(5))
    calls = []

    def grep(path, name, timeout):
        calls.append(name)
        return False

    r = ac.check(text, repos, grep=grep)
    assert (r.said, r.refuted, r.unclear, r.confirmed) == (5, 0, 2, 3)
    assert sorted(set(calls)) == ["Nombre0", "Nombre1", "Nombre2"]
    # el mismo nombre repetido no gasta otro cupo ni otro grep
    r2 = ac.check("no existe `Uno`. no existe `Uno`. no existe `Dos`. no existe `Tres`.", repos, grep=grep)
    assert (r2.said, r2.unclear) == (4, 0)


def test_tope_de_un_segundo(repos):
    ticks = iter([0.0, 0.2, 1.5, 1.5, 1.5, 1.5])
    clock = lambda: next(ticks)  # noqa: E731
    seen = []

    def grep(path, name, timeout):
        seen.append(round(timeout, 1))
        return False

    r = ac.check("no existe `A_a`. no existe `B_b`. no existe `C_c`.", {"pos": repos["pos"]}, grep=grep, clock=clock)
    # el primer nombre entra con lo que queda del presupuesto; el reloj supera 1 s y el resto no se evalúa
    assert seen == [0.8] and (r.said, r.confirmed, r.unclear) == (3, 1, 2)
    # el grep real respeta el timeout que recibe
    assert ac._git_grep(repos["pos"], "FooBar_x", 1.0) is True
    assert ac._git_grep(repos["pos"], "FooBar_x_no", 1.0) is False
    assert ac._git_grep(str(repos["pos"]) + "/no-existe", "x", 1.0) is None


# ---------------------------------------------------------------------------
# 2. registro: se completa después de mostrar, en memoria y en disco
# ---------------------------------------------------------------------------


@pytest.fixture
def wide(monkeypatch):
    """Rich corta a 80 columnas sin TTY: la tabla de salud necesita ancho para que las celdas no se partan."""
    monkeypatch.setenv("COLUMNS", "400")


@pytest.fixture
def out_dir(tmp_path, monkeypatch):
    out = tmp_path / "out"
    monkeypatch.setattr(config, "OUTPUT_DIR", out)
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects", raising=False)
    for key in ("HEALTH_MIN_N", "HEALTH_CACHE_MIN", "HEALTH_CITATIONS_MISSING_MAX", "HEALTH_COST_RISE_PCT"):
        monkeypatch.setattr(config, key, getattr(config, key), raising=False)
    return out


class _FakeAgent:
    def __init__(self, name, reply):
        self.name, self.reply = name, reply
        self.last_usage = {"agent": name, "model": "claude-haiku-4-5", "requests": 1, "input_tokens": 10,
                           "output_tokens": 5, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
                           "cache_creation_5m": 0, "cache_creation_1h": 0}

    def run(self, task):
        return self.reply


def _orch(monkeypatch, repos, reply):
    for key, value in (("USE_BEDROCK", False), ("EVALUATOR_ENABLED", False), ("META_AGENT_ENABLED", False),
                       ("REPOS", repos), ("SESSION_MAX_USD", 100.0), ("BURST_GAP_SECONDS", 0)):
        monkeypatch.setattr(config, key, value, raising=False)
    o = Orchestrator()
    o.quiet = True
    o.route_medians_fn = lambda: {}
    monkeypatch.setattr(o, "_get_agent", lambda name: _FakeAgent(name, reply))
    return o


def test_check_absences_completa_el_registro_en_disco_tras_mostrar(out_dir, repos, monkeypatch):
    o = _orch(monkeypatch, repos, "No hay nada en `FooBar_x`? Sí: no existe `FooBar_x` ni hay `OnlyInCore` en el pos.")
    shown = o.run("¿dónde está FooBar_x?")
    rec = o.last_run_record
    assert {rec["absences_said"], rec["absences_refuted"], rec["absences_unclear"]} == {0}  # aún no: se muestra primero
    result = o.check_absences(shown)
    assert result is not None and (result.said, result.refuted, result.unclear) == (2, 1, 1)
    assert rec["absences_said"] == 2 and rec["absences_refuted"] == 1
    on_disk = metrics.read_run_records(cfg=config)
    assert len(on_disk) == 1 and on_disk[0]["run_id"] == rec["run_id"]
    assert (on_disk[0]["absences_said"], on_disk[0]["absences_refuted"], on_disk[0]["absences_unclear"]) == (2, 1, 1)
    assert result.summary == "ausencias: 2 dichas · 1 refutadas · 1 no evaluables"
    # la respuesta sin negaciones no reescribe nada
    o2 = _orch(monkeypatch, repos, "Está en `FooBar_x` (pos/a.go:3).")
    o2.check_absences(o2.run("¿dónde está?"))
    assert o2.last_absences.said == 0 and o2.last_run_record["absences_said"] == 0
    agg = metrics.aggregate(metrics.read_run_records(cfg=config))
    assert (agg["absences_said"], agg["absences_refuted"], agg["absences_unclear"]) == (2, 1, 1)


def test_amend_run_record_sin_run_id_o_sin_registro(out_dir):
    assert metrics.amend_run_record("", {"a": 1}) is False
    assert metrics.amend_run_record("nope", {"a": 1}) is False
    metrics.write_run_record({"run_id": "r1", "ok": True})
    metrics.write_run_record({"run_id": "r2", "ok": True})
    assert metrics.amend_run_record("r1", {"absences_said": 3}) is True
    recs = {r["run_id"]: r for r in metrics.read_run_records(cfg=config)}
    assert recs["r1"]["absences_said"] == 3 and "absences_said" not in recs["r2"]


# ---------------------------------------------------------------------------
# 3. stats --salud
# ---------------------------------------------------------------------------

TODAY = date(2026, 11, 17)  # martes, semana ISO 47


def _record(day: str, i: int, **extra):
    base = dict(run_id=f"{day}-{i}", ts=f"{day}T10:00:00+00:00", source="repl", project="demo", router="info_direct",
                plan=[ARCH], ok=True, cost_usd=0.1, tokens={"input_tokens": 100, "cache_read_input_tokens": 900,
                "cache_creation_input_tokens": 0, "output_tokens": 10},
                citations_total=4, citations_missing=1, absences_said=2, absences_refuted=1, absences_unclear=0)
    base.update(extra)
    return base


def _write(out, day: str, n: int, start: int = 0, **extra):
    for i in range(start, start + n):
        metrics.append_private(out / "metrics" / "runs" / f"{day}.jsonl", json.dumps(_record(day, i, **extra)))


def _feedback(out, day: str, verdicts: list[str]):
    path = config.PROJECTS_DIR / "demo" / "evals" / "response_feedback.jsonl"
    for v in verdicts:
        metrics.append_private(path, json.dumps({"ts": f"{day}T12:00:00+00:00", "verdict": v}))


def test_semanas_iso_y_lectura_de_feedback(out_dir):
    assert health.iso_week_range(TODAY) == (date(2026, 11, 16), date(2026, 11, 22))
    assert health.iso_week_range(TODAY, -1) == (date(2026, 11, 9), date(2026, 11, 15))
    assert health.week_label(date(2026, 11, 16)) == "S47 2026"
    _feedback(out_dir, "2026-11-16", ["bien", "mal", "bien"])
    _feedback(out_dir, "2026-11-01", ["mal"])
    entries = health.read_response_feedback(config, date(2026, 11, 16), date(2026, 11, 22))
    assert [e["verdict"] for e in entries] == ["bien", "mal", "bien"]
    cats = metrics.health_categories([], entries)
    assert cats["bien_sobre_total"] == {"value": 0.6667, "n": 3} and cats["ausencias_refutadas_pct"] == {"value": None, "n": 0}


def test_salud_con_19_registros_dice_sin_datos_y_con_20_muestra_cifras(out_dir, wide):
    _write(out_dir, "2026-11-16", 19)
    runner = CliRunner()
    res = runner.invoke(minka_main.app, ["stats", "--salud", "--semana", "2026-11-17"])
    assert res.exit_code == 0, res.output
    flat = " ".join(res.output.split())
    assert "S47 2026" in flat and "S43 2026" in flat and "base v3" in flat
    assert "sin datos suficientes (n=19)" in flat and "$0.10" not in flat
    assert "n mínimo por categoría 20" in flat and "sin baseline_v3.json" in flat
    _write(out_dir, "2026-11-16", 1, start=19)
    res = runner.invoke(minka_main.app, ["stats", "--salud", "--semana", "2026-11-17"])
    flat = " ".join(res.output.split())
    assert "$0.10 (n=20)" in flat and "90% (n=20)" in flat  # costo mediano info_direct y caché
    assert "25% (n=80)" in flat  # 20 de 80 citas no encontradas
    assert "50% (n=40)" in flat  # 20 de 40 ausencias evaluables refutadas
    assert "sin datos suficientes (n=0)" not in flat and "— (n=0)" in flat  # llm/1 sin corridas: n visible


def test_salud_crudo_muestra_cifras_con_n_y_aviso(out_dir, wide):
    _write(out_dir, "2026-11-10", 3)
    _feedback(out_dir, "2026-11-10", ["bien", "bien", "mal"])
    res = CliRunner().invoke(minka_main.app, ["stats", "--salud", "--semana", "2026-11-17", "--crudo"])
    assert res.exit_code == 0, res.output
    flat = " ".join(res.output.split())
    assert "sin datos suficientes" not in flat
    assert "$0.10 (n=3) · muestra pequeña" in flat and "67% (n=3) · muestra pequeña" in flat
    assert "--crudo: cifras con muestra pequeña" in flat
    # y la base v3, si existe, ocupa la última columna con su n
    metrics.write_private(metrics.baseline_path(config), json.dumps({
        "range": ["2026-11-05", "2026-11-11"],
        "categories": {"corridas": {"value": 7, "n": 7}, "cache_mediana": {"value": 0.85, "n": 7}},
    }))
    res = CliRunner().invoke(minka_main.app, ["stats", "--salud", "--semana", "2026-11-17"])
    flat = " ".join(res.output.split())
    assert "base v3: 2026-11-05..2026-11-11 (7 corridas)" in flat and "sin datos suficientes (n=7)" in flat
    assert CliRunner().invoke(minka_main.app, ["stats", "--salud", "--semana", "ayer"]).exit_code == 2


# ---------------------------------------------------------------------------
# 4. doctor: salud no bloqueante
# ---------------------------------------------------------------------------


def _cache_tokens(ratio: float) -> dict:
    read = int(ratio * 1000)
    return {"input_tokens": 1000 - read, "cache_read_input_tokens": read, "cache_creation_input_tokens": 0, "output_tokens": 10}


def test_doctor_avisa_con_cache_070_y_calla_con_085(out_dir):
    _write(out_dir, "2026-11-15", 25, tokens=_cache_tokens(0.70), citations_total=0, citations_missing=0,
           absences_said=0, absences_refuted=0)
    alerts = {a["name"]: a for a in health.health_alerts(config, today=TODAY)}
    cache = alerts["Caché 7 días"]
    assert cache["ok"] is False and "70% (n=25)" in cache["detail"] and "mínimo 80%" in cache["detail"]
    assert alerts["Citas no encontradas 7 días"]["ok"] is True and "sin datos suficientes (n=0 < 20)" in alerts["Citas no encontradas 7 días"]["detail"]
    assert alerts["Costo mediano 7 días"]["ok"] is True and "sin datos suficientes" in alerts["Costo mediano 7 días"]["detail"]
    checks = {c.name: c for c in doctor.run_checks(config, now=datetime(2026, 11, 17, 12, tzinfo=timezone.utc))}
    assert checks["Caché 7 días"].ok is False and checks["Caché 7 días"].critical is False
    assert doctor.has_critical_failure(list(checks.values())) is doctor.has_critical_failure(
        [c for c in checks.values() if not c.name.endswith("7 días")]
    )
    # con 0.85 calla
    for p in (out_dir / "metrics" / "runs").glob("*.jsonl"):
        p.unlink()
    _write(out_dir, "2026-11-15", 25, tokens=_cache_tokens(0.85))
    alerts = {a["name"]: a for a in health.health_alerts(config, today=TODAY)}
    assert alerts["Caché 7 días"]["ok"] is True and "85% (n=25)" in alerts["Caché 7 días"]["detail"]
    # citas 25% > 5% avisa; ausencias 50% es informativo ("para revisar"), nunca alerta
    assert alerts["Citas no encontradas 7 días"]["ok"] is False
    abs_alert = alerts["Ausencias refutadas 7 días"]
    assert abs_alert["ok"] is True and abs_alert["informative"] is True and "50% (n=50) · para revisar" in abs_alert["detail"]
    abs_check = [c for c in doctor.run_checks(config, now=datetime(2026, 11, 17, 12, tzinfo=timezone.utc)) if c.name == "Ausencias refutadas 7 días"][0]
    assert abs_check.mark == "–" and abs_check.ok is True and abs_check.critical is False
    assert not hasattr(config, "HEALTH_ABSENCE_REFUTED_MAX")
    for p in (out_dir / "metrics" / "runs").glob("*.jsonl"):
        p.unlink()
    # el n es por categoría: 19 corridas con 4 citas cada una son 76 citas (sí alcanza), pero 19 cachés no
    _write(out_dir, "2026-11-15", 19, tokens=_cache_tokens(0.5))
    alerts = {a["name"]: a for a in health.health_alerts(config, today=TODAY)}
    assert alerts["Caché 7 días"]["ok"] is True and "sin datos suficientes (n=19 < 20)" in alerts["Caché 7 días"]["detail"]
    assert alerts["Citas no encontradas 7 días"]["ok"] is False and "(n=76)" in alerts["Citas no encontradas 7 días"]["detail"]
    for p in (out_dir / "metrics" / "runs").glob("*.jsonl"):
        p.unlink()
    _write(out_dir, "2026-11-15", 19, tokens=_cache_tokens(0.5), citations_total=1, citations_missing=1, absences_said=1, absences_refuted=1)
    alerts = {a["name"]: a for a in health.health_alerts(config, today=TODAY)}
    assert all(a["ok"] for a in alerts.values()) and all("sin datos suficientes" in a["detail"] for a in alerts.values())


def test_doctor_costo_sube_mas_de_25_pct(out_dir):
    _write(out_dir, "2026-11-05", 20, cost_usd=0.10)  # 7 anteriores: 2026-11-04..2026-11-10
    _write(out_dir, "2026-11-14", 20, cost_usd=0.13)  # últimos 7: 2026-11-11..2026-11-17
    alerts = {a["name"]: a for a in health.health_alerts(config, today=TODAY)}
    cost = alerts["Costo mediano 7 días"]
    assert cost["ok"] is False and "$0.13 (n=20) contra $0.10 (n=20)" in cost["detail"] and "+30%" in cost["detail"]
    _write(out_dir, "2026-11-13", 20, cost_usd=0.10)  # la mediana de los últimos 7 baja a 0.115
    alerts = {a["name"]: a for a in health.health_alerts(config, today=TODAY)}
    assert alerts["Costo mediano 7 días"]["ok"] is True


# ---------------------------------------------------------------------------
# 5. stats --purge --que
# ---------------------------------------------------------------------------


def test_purge_exige_que_y_simula_sin_yes(out_dir, wide):
    _write(out_dir, "2026-10-01", 2)
    _write(out_dir, "2026-11-15", 1)
    log = out_dir / "logs" / "agent_runs.jsonl"
    metrics.append_private(log, json.dumps({"ts": "2026-09-01T00:00:00+00:00"}))
    metrics.append_private(log, json.dumps({"ts": "2026-11-15T00:00:00+00:00"}))
    _feedback(out_dir, "2026-09-02", ["mal"])
    _feedback(out_dir, "2026-11-16", ["bien"])
    baseline = metrics.baseline_path(config)
    metrics.write_private(baseline, "{}")
    runner = CliRunner()
    res = runner.invoke(minka_main.app, ["stats", "--purge"])
    assert res.exit_code == 2 and "--que" in res.output
    assert runner.invoke(minka_main.app, ["stats", "--purge", "--que", "otra"]).exit_code == 2
    res = runner.invoke(minka_main.app, ["stats", "--purge", "--que", "logs"])
    assert res.exit_code == 0 and "Simulación" in res.output and "logs: 1 archivo(s), 2 línea(s)" in res.output
    assert "metricas" not in res.output and log.is_file()
    # --before aplica a lo elegido: en logs/feedback, por líneas (el archivo queda)
    res = runner.invoke(minka_main.app, ["stats", "--purge", "--que", "todo", "--before", "2026-11-01"])
    flat = " ".join(res.output.split())
    assert "metricas: 1 archivo(s), 2 línea(s)" in flat and "logs: 1 archivo(s), 1 línea(s)" in flat and "feedback: 1 archivo(s), 1 línea(s)" in flat
    assert len(list((out_dir / "metrics" / "runs").glob("*.jsonl"))) == 2 and len(log.read_text().splitlines()) == 2
    res = runner.invoke(minka_main.app, ["stats", "--purge", "--que", "todo", "--before", "2026-11-01", "--yes"])
    assert res.exit_code == 0 and "Borrados 3 archivo(s)" in res.output
    assert [p.name for p in (out_dir / "metrics" / "runs").glob("*.jsonl")] == ["2026-11-15.jsonl"]
    assert log.read_text().splitlines() == [json.dumps({"ts": "2026-11-15T00:00:00+00:00"})]
    fb = config.PROJECTS_DIR / "demo" / "evals" / "response_feedback.jsonl"
    assert len(fb.read_text().splitlines()) == 1 and baseline.exists()
    res = runner.invoke(minka_main.app, ["stats", "--purge", "--que", "feedback", "--yes"])
    assert "Borrados 1 archivo(s)" in res.output and not fb.exists() and log.is_file() and baseline.exists()
    with pytest.raises(ValueError):
        metrics.purge_plan(config, None, "nada")


# ---------------------------------------------------------------------------
# 6. clave ausente = sin dato; amend atómico con dos escritores; filtro de la base
# ---------------------------------------------------------------------------


def test_registros_antiguos_sin_la_clave_quedan_fuera_del_n():
    new = _record("2026-11-16", 0, interrupted=0, iterations_capped=1, ends_with_offer=False,
                  citations_symbol_far=1, citations_symbol_absent=0)
    old = {k: v for k, v in _record("2026-11-16", 1).items()
           if k not in ("absences_said", "absences_refuted", "absences_unclear", "citations_symbol_far", "citations_symbol_absent")}
    old.pop("interrupted", None)  # registro de 0.2.3: tampoco tiene interrupted/iterations_capped/ends_with_offer
    cats = metrics.health_categories([new, old])
    assert cats["corridas"]["n"] == 2 and cats["cache_mediana"]["n"] == 2
    assert cats["citas_no_encontradas_pct"] == {"value": 0.25, "n": 8}  # ambos la tienen
    assert cats["citas_simbolo_lejano_o_ausente_pct"] == {"value": 0.25, "n": 4}  # solo el nuevo
    assert cats["ausencias_refutadas_pct"] == {"value": 0.5, "n": 2}  # solo el nuevo
    assert cats["interrumpidas_pct"] == {"value": 0.0, "n": 1} and cats["vueltas_con_tope_pct"] == {"value": 1.0, "n": 1}
    assert cats["cierres_con_oferta_pct"] == {"value": 0.0, "n": 1}
    only_old = metrics.health_categories([old])
    assert only_old["ausencias_refutadas_pct"] == {"value": None, "n": 0} and only_old["interrumpidas_pct"] == {"value": None, "n": 0}
    assert health.fmt_cell(only_old["ausencias_refutadas_pct"], "pct", 20, False) == "— (n=0)"


_WRITER = """
import json, sys
from pathlib import Path
from types import SimpleNamespace
from minka.core import metrics
cfg = SimpleNamespace(OUTPUT_DIR=Path(sys.argv[1]))
tag, parity = sys.argv[2], int(sys.argv[3])
for round_ in range(20):
    for i in range(parity, 40, 2):
        assert metrics.amend_run_record(f"r{i}", {tag: round_ + 1}, cfg), (tag, i)
    metrics.append_private(metrics.runs_dir(cfg) / (metrics.local_day() + ".jsonl"), json.dumps({"run_id": f"{tag}-new-{round_}", "ok": True}))
"""


def test_amend_con_dos_procesos_no_pierde_lineas(out_dir):
    import sys

    for i in range(40):
        metrics.write_run_record({"run_id": f"r{i}", "ok": True})
    procs = [subprocess.Popen([sys.executable, "-c", _WRITER, str(out_dir), tag, str(parity)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE) for tag, parity in (("a", 0), ("b", 1))]
    outs = [p.communicate(timeout=120) for p in procs]
    assert all(p.returncode == 0 for p in procs), [o[1].decode()[-800:] for o in outs]
    recs = {r["run_id"]: r for r in metrics.read_run_records(cfg=config)}
    assert len(recs) == 40 + 40  # ninguna línea perdida ni duplicada
    assert all(recs[f"r{i}"]["a"] == 20 for i in range(0, 40, 2)) and all(recs[f"r{i}"]["b"] == 20 for i in range(1, 40, 2))
    assert all(("b" not in recs[f"r{i}"]) for i in range(0, 40, 2))
    path = metrics.runs_dir(config) / f"{metrics.local_day()}.jsonl"
    assert oct(path.stat().st_mode & 0o777) == "0o600" and not list(path.parent.glob(".*.tmp"))
    assert (path.parent / (path.name + ".lock")).exists() and oct((path.parent / (path.name + ".lock")).stat().st_mode & 0o777) == "0o600"


def test_baseline_guarda_source_filter_y_salud_avisa_si_difiere(out_dir, wide):
    _write(out_dir, "2026-11-06", 3)
    runner = CliRunner()
    assert runner.invoke(minka_main.app, ["stats", "--freeze-baseline"]).exit_code == 0
    data = json.loads(metrics.baseline_path(config).read_text())
    assert data["source_filter"] == {"ok_only": True, "excluded_sources": ["eval_quality"]}
    assert all("n" in c for c in data["categories"].values())
    res = runner.invoke(minka_main.app, ["stats", "--salud", "--semana", "2026-11-17"])
    assert res.exit_code == 0 and "⚠ la base v3" not in res.output
    # base congelada con otro filtro (o sin registrarlo): aviso, la tabla sigue saliendo
    data["source_filter"] = {"ok_only": False, "excluded_sources": []}
    metrics.write_private(metrics.baseline_path(config), json.dumps(data))
    flat = " ".join(runner.invoke(minka_main.app, ["stats", "--salud", "--semana", "2026-11-17"]).output.split())
    assert "⚠ la base v3 usa un filtro de fuentes distinto" in flat and "no es válida" in flat and "base v3: 2026-11-05..2026-11-11" in flat
    del data["source_filter"]
    metrics.write_private(metrics.baseline_path(config), json.dumps(data))
    flat = " ".join(runner.invoke(minka_main.app, ["stats", "--salud", "--semana", "2026-11-17"]).output.split())
    assert "no registró su filtro de fuentes" in flat
    assert health.baseline_filter_matches(None) is None and health.baseline_filter_matches({"source_filter": dict(metrics.SOURCE_FILTER)}) is True
