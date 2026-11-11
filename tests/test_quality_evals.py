"""`minka evals quality`: tipos con verificación determinista sobre repos temporales, caducidad por
commit, no evaluable, tope proyectado, sesión limpia por corrida, fuente excluida de stats, casos
antiguos válidos, --refresh y --draft. Sin red: runner falso."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from minka import main as minka_main
from minka._app import SOURCE_ENV
from minka.config_loader import config
from minka.core import metrics
from minka.core.evaluator import quality_evals as qe
from minka.core.evaluator.regression_evals import EvalCase, load_golden_set

MEDIANS = {"info_direct": 0.10, "llm": 0.40, "forced": 0.20, "other": 0.20}


def _git(path, *args):
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


@pytest.fixture
def repos(tmp_path):
    api = tmp_path / "svc-api"
    (api / "service").mkdir(parents=True)
    (api / "service" / "token.go").write_text(
        "package service\n\nimport \"net/http\"\n\n// ResolveToken pide el token al SSO\nfunc ResolveToken() string {\n"
        "\thttp.Post(\"/logon\", \"\", nil)\n\treturn \"\"\n}\n\nfunc HandleA() {}\nfunc HandleB() {}\n"
    )
    (api / "service" / "other.go").write_text("package service\n\nfunc Other() {}\n")
    _git(api, "init", "-q", "-b", "main")
    _git(api, "config", "user.email", "t@example.com")
    _git(api, "config", "user.name", "t")
    _git(api, "add", "-A")
    _git(api, "commit", "-q", "-m", "init")
    return {"api": str(api)}


def _case(**kw) -> EvalCase:
    base = {"id": "c1", "input": "¿dónde se resuelve el token?", "agent": "architecture_agent"}
    base.update(kw)
    return EvalCase.model_validate(base)


def _runner(outputs):
    """Runner falso: devuelve las salidas en orden, costo fijo, ruta info_direct."""
    it = iter(outputs)
    calls = []

    def run(case):
        calls.append(case.id)
        return next(it), 0.05, "info_direct"

    run.calls = calls
    return run


# ---------------------------------------------------------------------------
# tipos
# ---------------------------------------------------------------------------


def test_existence_acierto_fallo_y_no_evaluable(repos):
    c = _case(type="existence", expect={"grep": "/logon", "fixed": True, "must_say": "existe", "term": "/logon"})
    assert qe.score(c, "Sí: el cliente llama a `/logon` en svc-api/service/token.go:7.", repos).verdict == qe.VERDICT_OK
    assert qe.score(c, "No existe ningún cliente para /logon. [NO ENCONTRADO EN CÓDIGO]", repos).verdict == qe.VERDICT_FAIL
    assert qe.score(c, "Depende de la configuración del entorno.", repos).verdict == qe.VERDICT_NA
    neg = _case(type="existence", expect={"grep": "/refund", "fixed": True, "must_say": "no_existe", "term": "/refund"})
    assert qe.score(neg, "No hay ningún handler para /refund. [NO ENCONTRADO EN CÓDIGO]", repos).verdict == qe.VERDICT_OK


def test_location_cita_correcta_archivo_equivocado_y_sin_citas(repos):
    c = _case(type="location", expect={"file": "svc-api/service/token.go", "symbol": "ResolveToken"})
    assert qe.score(c, "Se resuelve en `ResolveToken()` (svc-api/service/token.go:6).", repos).verdict == qe.VERDICT_OK
    assert qe.score(c, "Se resuelve en api/service/token.go:7.", repos).verdict == qe.VERDICT_OK  # alias + ventana
    r = qe.score(c, "Está en svc-api/service/other.go:3.", repos)
    assert r.verdict == qe.VERDICT_FAIL and "ninguna cita" in r.note
    r = qe.score(c, "Se resuelve en svc-api/service/token.go:40.", repos)  # fuera de rango de líneas
    assert r.verdict == qe.VERDICT_FAIL
    assert qe.score(c, "Creo que en el servicio de tokens.", repos).verdict == qe.VERDICT_NA


def test_count_con_tolerancia_y_ambiguo(repos):
    c = _case(type="count", expect={"grep": "^func Handle", "tolerance": 0})
    assert qe.score(c, "Hay 2 handlers (svc-api/service/token.go:11).", repos).verdict == qe.VERDICT_OK
    assert qe.score(c, "Hay 3 handlers.", repos).verdict == qe.VERDICT_FAIL
    assert qe.score(c, "Hay 2 handlers y 4 helpers.", repos).verdict == qe.VERDICT_NA
    assert qe.score(_case(type="count", expect={"grep": "^func Handle", "tolerance": 1}), "Hay 3 handlers.", repos).verdict == qe.VERDICT_OK


def test_absence(repos):
    c = _case(type="absence", expect={})
    assert qe.score(c, "[NO ENCONTRADO EN CÓDIGO]: no hay RebuildLedger.", repos).verdict == qe.VERDICT_OK
    assert qe.score(c, "RebuildLedger vive en svc-api/service/token.go:6.", repos).verdict == qe.VERDICT_FAIL
    assert qe.score(c, "Probablemente en el módulo de ledger.", repos).verdict == qe.VERDICT_NA


def test_consistency_entre_corridas(repos):
    c = _case(type="consistency", expect={"min_jaccard": 1.0})
    same = "Ver svc-api/service/token.go:6."
    v, unstable = qe.consolidate(c, [qe.score(c, same, repos), qe.score(c, same, repos)])
    assert v == qe.VERDICT_OK and not unstable
    v, unstable = qe.consolidate(c, [qe.score(c, same, repos), qe.score(c, "Ver svc-api/service/other.go:3.", repos)])
    assert v == qe.VERDICT_FAIL and unstable
    assert qe.consolidate(c, [qe.score(c, "sin citas", repos)])[0] == qe.VERDICT_NA


def test_legacy_casos_antiguos_siguen_validos(demo_project):
    cases = load_golden_set("demo", projects_dir=demo_project.PROJECTS_DIR)
    assert cases and all(c.type == "legacy" and c.review == "revisado" for c in cases)
    c = cases[0]
    full = " ".join(c.must_mention)
    assert qe.score(c, f"Respuesta que menciona {full}.", {}).verdict == qe.VERDICT_OK
    assert qe.score(c, "Respuesta vacía.", {}).verdict == (qe.VERDICT_FAIL if c.must_mention else qe.VERDICT_OK)


# ---------------------------------------------------------------------------
# validación en frío: caducado, sin revisar
# ---------------------------------------------------------------------------


def test_caducado_por_commit_y_archivo_cambiado(repos):
    api = Path(repos["api"])
    old = qe.git_head(api)
    c = _case(type="location", expect={"file": "svc-api/service/token.go", "symbol": "ResolveToken"}, repo_commit={"api": old})
    assert qe.validate(c, repos) == (qe.STATE_OK, "")
    (api / "service" / "token.go").write_text("package service\n\nfunc ResolveToken() string { return \"\" }\n")
    _git(api, "commit", "-qam", "mueve")
    state, note = qe.validate(c, repos)
    assert state == qe.STATE_STALE and "HEAD" in note
    # Si cambió el repo pero no el archivo del caso, sigue ok.
    (api / "service" / "other.go").write_text("package service\n\nfunc Other2() {}\n")
    _git(api, "commit", "-qam", "otro")
    c2 = _case(type="location", expect={"file": "svc-api/service/token.go", "symbol": "ResolveToken"}, repo_commit={"api": qe.git_head(api)})
    (api / "service" / "other.go").write_text("package service\n\nfunc Other3() {}\n")
    _git(api, "commit", "-qam", "otro2")
    assert qe.validate(c2, repos)[0] == qe.STATE_OK


def test_caducado_por_simbolo_o_patron_que_ya_no_se_sostiene(repos):
    api = Path(repos["api"])
    c = _case(type="location", expect={"file": "svc-api/service/token.go", "symbol": "Desaparecido"})
    assert qe.validate(c, repos)[0] == qe.STATE_STALE
    e = _case(type="existence", expect={"grep": "/refund", "fixed": True, "must_say": "existe"})
    assert qe.validate(e, repos)[0] == qe.STATE_STALE  # el esperado dice que existe y no existe
    assert qe.validate(_case(type="absence"), repos)[0] == qe.STATE_OK
    (api / "x.go").write_text("x")  # repo_commit solo aplica a location/consistency
    assert qe.validate(_case(type="existence", expect={"grep": "/logon", "fixed": True}, repo_commit={"api": "deadbeef0000"}), repos)[0] == qe.STATE_OK


def test_location_exige_simbolo_definido_no_solo_mencionado(repos):
    api = Path(repos["api"])
    (api / "service" / "caller.go").write_text(
        "package service\n\n// usa ResolveToken pero no lo define\nfunc Caller() string { return ResolveToken() }\n"
    )
    _git(api, "add", "-A")
    _git(api, "commit", "-qm", "caller")
    mentioned = _case(type="location", expect={"file": "svc-api/service/caller.go", "symbol": "ResolveToken"})
    state, note = qe.validate(mentioned, repos)
    assert state == qe.STATE_STALE and "DEFINIDO" in note
    defined = _case(type="location", expect={"file": "svc-api/service/token.go", "symbol": "ResolveToken"})
    assert qe.validate(defined, repos) == (qe.STATE_OK, "")
    assert qe.validate(_case(type="location", expect={"file": "svc-api/service/caller.go", "symbol": "Caller"}), repos)[0] == qe.STATE_OK


def test_symbol_defined_formas():
    go = "package p\n\ntype Kind int\n\nconst (\n\tKindA Kind = iota\n\tLimit = 3\n)\n\nvar names = []string{}\n\nfunc (k Kind) String() string { return \"\" }\nfunc Plain() {}\n"
    for sym in ("Kind", "KindA", "Limit", "names", "String", "Plain"):
        assert qe.symbol_defined(go, sym), sym
    assert not qe.symbol_defined(go, "Other") and not qe.symbol_defined("x := Plain()\n", "Plain")
    assert qe.symbol_defined("def helper(x):\n    pass\n", "helper") and qe.symbol_defined("export const token = 1;\n", "token")


def test_sin_revisar_no_se_puntua(repos):
    c = _case(type="absence", review="sin revisar")
    report = qe.evaluate([c], repos, runs=1, max_usd=6, runner=_runner(["x"]), medians=MEDIANS)
    assert report["sin_revisar"] == 1 and report["evaluables"] == 0 and report["spent_usd"] == 0


# ---------------------------------------------------------------------------
# tope proyectado, gasto real, reporte
# ---------------------------------------------------------------------------


def test_tope_proyectado_corta_antes_de_gastar(repos):
    cases = [_case(id="a", type="absence"), _case(id="b", type="absence", input="¿qué pasa si luego falla y además reintenta?")]
    runner = _runner(["[NO ENCONTRADO]", "[NO ENCONTRADO]"])
    report = qe.evaluate(cases, repos, runs=3, max_usd=0.25, runner=runner, medians=MEDIANS)
    # a: 3 × $0.10 proyectado = $0.30 > $0.25 → ni se ejecuta; b tampoco.
    assert runner.calls == [] and report["omitidos_por_tope"] == 2 and "proyectado" in report["stopped"]
    report = qe.evaluate(cases, repos, runs=1, max_usd=0.30, runner=runner, medians=MEDIANS)
    assert runner.calls == ["a"] and report["aciertos"] == 1 and report["omitidos_por_tope"] == 1  # b va por llm ($0.40)


def test_gasto_real_corta_y_reporte_sin_respuestas(repos):
    outputs = ["[NO ENCONTRADO]"] * 6

    def runner(case):
        return outputs.pop(0), 0.30, "llm"

    cases = [_case(id=f"c{i}", type="absence") for i in range(3)]
    report = qe.evaluate(cases, repos, runs=1, max_usd=0.50, runner=runner, medians=MEDIANS)
    assert report["spent_usd"] == pytest.approx(0.60) and "superado" in report["stopped"]
    assert report["omitidos_por_tope"] == 1 and report["aciertos"] == 2
    raw = json.dumps(report)
    assert "NO ENCONTRADO" not in raw and report["tasa_acierto"] == 1.0
    text = qe.format_report(report)
    assert "detenido" in text and "c0" in text


def test_mediana_e_inestable(repos):
    c = _case(type="absence", runs=3)
    runner = _runner(["[NO ENCONTRADO]", "Está en svc-api/service/token.go:6.", "[NO ENCONTRADO]"])
    report = qe.evaluate([c], repos, runs=1, max_usd=6, runner=runner, medians=MEDIANS)
    r = report["results"][0]
    assert r["verdict"] == qe.VERDICT_OK and r["unstable"] and report["inestables"] == 1 and len(r["runs"]) == 3


# ---------------------------------------------------------------------------
# sesión limpia, fuente y stats
# ---------------------------------------------------------------------------


def test_default_runner_sesion_nueva_y_source(monkeypatch):
    seen = {}

    class _Orch:
        def __init__(self):
            self.memory, self.quiet, self.source = [], False, "cli"

        def run(self, task, forced_agent=None):
            seen["source"] = self.source
            seen["env"] = __import__("os").environ.get(SOURCE_ENV)
            seen["memory"] = len(self.memory)
            seen["forced"] = forced_agent
            self.last_run_record = {"cost_usd": 0.07, "router": "forced" if forced_agent else "info_direct"}
            return "ok"

    import minka.core.orchestrator.orchestrator as om

    monkeypatch.setattr(om, "Orchestrator", _Orch)
    monkeypatch.delenv(SOURCE_ENV, raising=False)
    out, cost, router = qe.default_runner(_case(type="absence", forced_agent="architecture"))
    assert (out, cost, router) == ("ok", 0.07, "forced")
    assert seen == {"source": qe.SOURCE, "env": qe.SOURCE, "memory": 0, "forced": "architecture"}
    assert __import__("os").environ.get(SOURCE_ENV) is None  # el entorno vuelve a como estaba


def test_stats_excluye_eval_quality_salvo_que_se_pida():
    base = {"ok": True, "cost_usd": 1.0, "tokens": {}, "per_agent": [], "router": "llm"}
    recs = [{**base, "source": "repl"}, {**base, "source": qe.SOURCE}]
    assert metrics.aggregate(recs)["runs"] == 1
    assert metrics.aggregate(recs, source=qe.SOURCE)["runs"] == 1
    assert metrics.aggregate(recs, source="repl")["runs"] == 1


def test_medianas_por_ruta_ignoran_evals(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path, raising=False)
    runs = tmp_path / "metrics" / "runs"
    runs.mkdir(parents=True)
    day = metrics.local_day()
    rows = [{"ok": True, "router": "info_direct", "cost_usd": 0.1, "source": "repl"},
            {"ok": True, "router": "info_direct", "cost_usd": 0.3, "source": "repl"},
            {"ok": True, "router": "info_direct", "cost_usd": 9.0, "source": qe.SOURCE}]
    (runs / f"{day}.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    med = qe.route_medians()
    assert med["info_direct"] == pytest.approx(0.2) and med["llm"] == qe.DEFAULT_ROUTE_COST["llm"]


def test_ruta_esperada_y_proyeccion():
    assert qe.expected_route(_case(type="absence", input="¿dónde está el token?")) == "info_direct"
    assert qe.expected_route(_case(type="absence", input="¿dónde está el token? ¿y luego quién lo usa?")) == "llm"
    assert qe.expected_route(_case(type="absence", forced_agent="architecture")) == "forced"
    assert qe.projected_cost(_case(type="absence", input="¿dónde está X?"), 3, MEDIANS) == pytest.approx(0.30)


# ---------------------------------------------------------------------------
# plantilla, --refresh, --draft y CLI
# ---------------------------------------------------------------------------


def test_plantilla_generica_25_casos_sin_empresa():
    cases = qe.load_cases("", config, template=True)
    assert len(cases) == 25
    from collections import Counter

    assert Counter(c.type for c in cases) == {"existence": 6, "location": 6, "count": 4, "absence": 5, "consistency": 4}
    raw = qe.TEMPLATE_PATH.read_text(encoding="utf-8").lower()
    # Nombres de empresa/repos reales, construidos por partes para que el propio test no los contenga.
    forbidden = ["ku" + "shki", "credi" + "banco", "am" + "ex", "us" + "rv", "bill" + "pocket"]
    assert all(w not in raw for w in forbidden)
    assert all(c.type in COMMIT_TYPES or not c.repo_commit for c in cases) if (COMMIT_TYPES := qe.COMMIT_TYPES) else True


def test_refresh_y_draft(repos, tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    evals = projects / "demo" / "evals"
    evals.mkdir(parents=True)
    kn = projects / "demo" / "knowledge" / "extracted"
    kn.mkdir(parents=True)
    (kn / "interfaces.md").write_text("El token se resuelve en `ResolveToken()` (svc-api/service/token.go:6).\n"
                                      "El helper `Other` no tiene cita.\n`Fantasma_x` tampoco existe.\n")
    (evals / "cases.json").write_text(json.dumps({"cases": [
        {"id": "loc", "type": "location", "input": "x", "expect": {"file": "svc-api/service/token.go", "symbol": "ResolveToken"}},
        {"id": "ex", "type": "existence", "input": "x", "expect": {"grep": "/logon", "fixed": True}},
    ]}))
    monkeypatch.setattr(config, "PROJECTS_DIR", projects, raising=False)
    monkeypatch.setattr(config, "REPOS", repos, raising=False)
    ok, msg = qe.refresh_commit(config, "demo", "loc")
    assert ok and "api@" in msg
    data = json.loads((evals / "cases.json").read_text())
    assert data["cases"][0]["repo_commit"] == {"api": qe.git_head(Path(repos["api"]))}
    ok, msg = qe.refresh_commit(config, "demo", "ex")
    assert not ok and "solo aplica" in msg
    new, msg = qe.draft_cases(config, "demo")
    assert len(new) == 3 and all(c["review"] == "sin revisar" for c in new)
    by_type = {c["type"] for c in new}
    assert by_type == {"location", "existence"}
    assert sum(1 for c in new if c["valid_now"]) == 2 and "3 borrador(es)" in msg  # Fantasma_x no se sostiene
    data = json.loads((evals / "cases.json").read_text())
    assert len(data["cases"]) == 5
    # Sin revisar no se puntúa, aunque se cargue.
    cases = load_golden_set("demo", projects)
    report = qe.evaluate(cases, repos, runs=1, max_usd=6, runner=_runner(["x"] * 9), medians=MEDIANS)
    assert report["sin_revisar"] == 3


def test_cli_quality_json_sin_modelo(repos, tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    (projects / "demo" / "evals").mkdir(parents=True)
    (projects / "demo" / "evals" / "cases.json").write_text(json.dumps({"cases": [
        {"id": "a", "type": "absence", "input": "¿dónde está RebuildLedger?"}]}))
    monkeypatch.setattr(config, "PROJECTS_DIR", projects, raising=False)
    monkeypatch.setattr(config, "ACTIVE_PROJECT", "demo", raising=False)
    monkeypatch.setattr(config, "REPOS", repos, raising=False)
    monkeypatch.setattr(config, "USE_BEDROCK", False, raising=False)
    monkeypatch.setattr(minka_main, "_require_llm_backend", lambda: None)
    monkeypatch.setattr(qe, "default_runner", lambda case: ("[NO ENCONTRADO EN CÓDIGO]", 0.02, "info_direct"))
    monkeypatch.setattr(qe, "route_medians", lambda *a, **k: MEDIANS)
    out = tmp_path / "r.json"
    result = CliRunner().invoke(minka_main.app, ["evals", "quality", "--json-out", str(out)])
    assert result.exit_code == 0, result.output
    report = json.loads(out.read_text())
    assert report["aciertos"] == 1 and report["spent_usd"] == pytest.approx(0.02) and report["max_usd"] == 6.0
    assert "evals quality" in result.output and "acierto" in result.output
    result = CliRunner().invoke(minka_main.app, ["evals", "quality", "--template", "--max-usd", "0.01"])
    assert result.exit_code == 3  # tope proyectado: no se ejecuta nada
