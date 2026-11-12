"""Lote 6 (operación): hermeticidad de la suite, `minka --version`, resultado parcial ante errores
de red/timeout/throttling/credenciales, respuesta vacía, propagación al orquestador, métricas,
`minka stats`, evals quality y la CLI/REPL. Cliente LLM falso en todos los casos."""

from __future__ import annotations

import os
import subprocess
from importlib.metadata import PackageNotFoundError
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx2 as httpx  # anthropic 2.x usa httpx2 (sin httpx en el entorno)
import pytest
from botocore import exceptions as be
from typer.testing import CliRunner

from minka import _app
from minka import main as minka_main
from minka._app import APP_NAME, DATA_DIR_ENV
from minka.config_loader import config
from minka.core import metrics
from minka.core.agents import base_agent
from minka.core.agents.base_agent import EMPTY_RESPONSE, INTERRUPTED_PREFIX, BaseAgent
from minka.core.evaluator import quality_evals as qe
from minka.core.evaluator.regression_evals import EvalCase
from minka.core.orchestrator import planner
from minka.core.orchestrator.orchestrator import Orchestrator

# ---------------------------------------------------------------------------
# 1. hermeticidad
# ---------------------------------------------------------------------------


def test_suite_no_ve_el_entorno_del_dev():
    minka_vars = [k for k in os.environ if k.startswith(f"{APP_NAME.upper()}_") and k != DATA_DIR_ENV]
    assert minka_vars == []
    assert not [k for k in os.environ if k.startswith("AWS_")]
    assert "ANTHROPIC_API_KEY" not in os.environ
    assert Path(os.environ["HOME"]).name.startswith(f"{APP_NAME}-home-")
    assert Path(os.environ[DATA_DIR_ENV]).name.startswith(f"{APP_NAME}-tests-")
    assert Path(os.environ[DATA_DIR_ENV]).exists()


def test_variable_dejada_por_un_test_no_sobrevive_al_siguiente():
    # El primer test del par la deja puesta sin monkeypatch; el fixture autouse la quita.
    os.environ["MINKA_QUIET"] = "1"
    os.environ["AWS_PROFILE"] = "x"


def test_variable_dejada_por_un_test_no_sobrevive_al_siguiente_2():
    assert "MINKA_QUIET" not in os.environ and "AWS_PROFILE" not in os.environ


# ---------------------------------------------------------------------------
# 2. --version
# ---------------------------------------------------------------------------


def _invoke(args, **kw):
    return CliRunner().invoke(minka_main.app, args, **kw)


def test_version_con_direct_url_git(monkeypatch):
    monkeypatch.setattr(_app, "_direct_url", lambda: {"url": "https://example.invalid/r.git",
                                                     "vcs_info": {"vcs": "git", "commit_id": "a86d27d0000000000000", "requested_revision": "v0.2.1"}})
    monkeypatch.setattr(minka_main, "_require_llm_backend", lambda: pytest.fail("--version no debe tocar el backend"))
    res = _invoke(["--version"])
    assert res.exit_code == 0
    assert "commit a86d27d00000" in res.output and "requested_revision: v0.2.1" in res.output
    assert res.output.startswith(APP_NAME)


def test_version_sin_direct_url_dice_editable(monkeypatch):
    monkeypatch.setattr(_app, "_direct_url", lambda: None)
    assert "instalación editable" in _app.version_text()
    monkeypatch.setattr(_app, "_direct_url", lambda: {"url": "file:///tmp/x.whl"})
    assert "sin control de versiones" in _app.version_text()


def test_version_sin_paquete_instalado(monkeypatch):
    import importlib.metadata as im

    def boom(name):
        raise PackageNotFoundError(name)

    monkeypatch.setattr(im, "version", boom)
    assert "sin paquete instalado" in _app.version_text()


# ---------------------------------------------------------------------------
# 3. fallos con resultado parcial (cliente falso)
# ---------------------------------------------------------------------------


class _Agent(BaseAgent):
    def get_system_prompt(self) -> str:
        return "sys"


class _FakeClient:
    """Devuelve las respuestas en orden; una excepción en la lista se lanza."""

    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = 0
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.calls += 1
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


def _text(text, stop="end_turn"):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason=stop,
                           usage=SimpleNamespace(input_tokens=100, output_tokens=10), model="m")


def _tool_use(text="Leí el archivo a.go."):
    blocks = [SimpleNamespace(type="text", text=text),
              SimpleNamespace(type="tool_use", name="read_file", input={"path": "a.go"}, id="t1")]
    return SimpleNamespace(content=blocks, stop_reason="tool_use",
                           usage=SimpleNamespace(input_tokens=100, output_tokens=10), model="m")


@pytest.fixture
def agente(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path / "out")
    monkeypatch.setattr(config, "QUIET", True, raising=False)
    monkeypatch.setattr(config, "PROGRESS_ENABLED", False, raising=False)
    monkeypatch.setattr(config, "AWS_PROFILE", "perfil-demo", raising=False)
    monkeypatch.setattr(base_agent.console, "print", lambda *a, **k: None)

    def make(steps):
        a = _Agent.__new__(_Agent)  # sin __init__: ni cliente real, ni knowledge, ni chroma
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
        a.recuerdos = []
        monkeypatch.setattr(a, "_recall_memories", lambda task: "", raising=False)
        monkeypatch.setattr(a, "_system_param", lambda: "sys", raising=False)
        monkeypatch.setattr(a, "_remember", lambda t, r: a.recuerdos.append(r), raising=False)
        monkeypatch.setattr(a, "_execute_tools", lambda resp: [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}], raising=False)
        return a

    return make


def _req():
    return httpx.Request("POST", "https://example.invalid")


def _resp(status):
    return httpx.Response(status, request=_req())


def test_timeout_en_segunda_vuelta_devuelve_parcial(agente):
    a = agente([_tool_use(), anthropic.APITimeoutError(request=_req())])
    out = a.run("analiza")
    assert "Leí el archivo a.go." in out
    assert out.rstrip().endswith(f"{INTERRUPTED_PREFIX} timeout, 1 vueltas completadas.")
    assert a.last_usage["interrupted"] == 1 and a.last_usage["interrupted_kind"] == "timeout"
    assert a.last_usage["requests"] == 1 and a.last_usage["input_tokens"] == 100  # costo de lo gastado
    assert a.recuerdos == []  # el parcial no se memoriza
    assert a.client.calls == 2  # sin reintentos propios


@pytest.mark.parametrize(
    ("exc", "kind"),
    [
        (anthropic.APIConnectionError(request=_req()), "red"),
        (anthropic.RateLimitError("429", response=_resp(429), body=None), "throttling"),
        (anthropic.InternalServerError("500", response=_resp(500), body=None), "servicio"),
        (be.ClientError({"Error": {"Code": "ThrottlingException", "Message": ""}}, "InvokeModel"), "throttling"),
        (be.ClientError({"Error": {"Code": "ExpiredTokenException", "Message": ""}}, "InvokeModel"), "credenciales"),
        (be.EndpointConnectionError(endpoint_url="https://example.invalid"), "red"),
        (be.ReadTimeoutError(endpoint_url="https://example.invalid"), "timeout"),
        (be.NoCredentialsError(), "credenciales"),
    ],
)
def test_clasificacion_de_errores_transitorios(agente, exc, kind):
    a = agente([exc])
    out = a.run("analiza")
    assert out == f"{INTERRUPTED_PREFIX} {kind}, 0 vueltas completadas." + (
        " Renueva con aws sso login --profile perfil-demo" if kind == "credenciales" else "")
    assert a.last_usage["interrupted"] == 1 and a.client.calls == 1


def test_sso_vencido_sin_traceback_con_perfil(agente):
    a = agente([be.UnauthorizedSSOTokenError()])
    out = a.run("analiza")
    assert "credenciales" in out and "aws sso login --profile perfil-demo" in out
    assert "Traceback" not in out


def test_type_error_y_ctrl_c_siguen_subiendo(agente):
    with pytest.raises(TypeError):
        agente([TypeError("mal argumento")]).run("analiza")
    with pytest.raises(KeyboardInterrupt):
        agente([KeyboardInterrupt()]).run("analiza")
    a = agente([anthropic.BadRequestError("400", response=_resp(400), body=None)])
    with pytest.raises(anthropic.BadRequestError):  # error de programación/petición: no es transitorio
        a.run("analiza")


def test_respuesta_vacia_es_fallo_no_util(agente):
    a = agente([SimpleNamespace(content=[], stop_reason="end_turn",
                                usage=SimpleNamespace(input_tokens=5, output_tokens=0), model="m")])
    out = a.run("analiza")
    assert out == EMPTY_RESPONSE and a.last_usage["empty_response"] == 1 and a.recuerdos == []


def test_respuesta_normal_sigue_igual(agente):
    a = agente([_tool_use(), _text("Listo: a.go:1.")])
    assert a.run("analiza") == "Listo: a.go:1."
    assert a.last_usage["interrupted"] == 0 and a.last_usage["empty_response"] == 0 and a.recuerdos


# ---------------------------------------------------------------------------
# 3b. orquestador: el parcial no es turno útil, corta el plan y la corrida no es ok
# ---------------------------------------------------------------------------


class _FakeAgent:
    def __init__(self, name, reply, interrupted=0, empty=0):
        self.name, self.reply = name, reply
        self.last_usage = {"agent": name, "model": "claude-haiku-4-5", "requests": 1, "input_tokens": 10,
                           "output_tokens": 5, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
                           "cache_creation_5m": 0, "cache_creation_1h": 0, "interrupted": interrupted,
                           "empty_response": empty}
        self.ran = 0

    def run(self, task):
        self.ran += 1
        return self.reply


@pytest.fixture
def orch(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "USE_BEDROCK", False)
    monkeypatch.setattr(config, "EVALUATOR_ENABLED", False)
    monkeypatch.setattr(config, "META_AGENT_ENABLED", False)
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path / "out")
    monkeypatch.setattr(config, "REPOS", {}, raising=False)
    o = Orchestrator()
    monkeypatch.setattr(o, "_stderr", SimpleNamespace(print=lambda *a, **k: None))
    return o


def test_parcial_no_es_turno_util_y_la_corrida_no_es_ok(orch, monkeypatch):
    partial = f"Leí a.go.\n\n{INTERRUPTED_PREFIX} timeout, 1 vueltas completadas."
    agents = {"architecture_agent": _FakeAgent("architecture_agent", partial, interrupted=1)}
    monkeypatch.setattr(orch, "_get_agent", lambda name: agents[name])
    out = orch.run("analiza el flujo de pagos", use_llm_planner=False, forced_agent="architecture_agent")
    assert INTERRUPTED_PREFIX in out
    assert orch.last_run_ok is False and orch.last_exit_code == 1
    assert orch.memory.as_messages() == []  # no quedó como turno útil
    assert orch.last_usage["totals"]["interrupted"] == 1


def test_plan_de_dos_pasos_se_corta_tras_el_parcial(orch, monkeypatch):
    partial = f"{INTERRUPTED_PREFIX} red, 0 vueltas completadas."
    agents = {"architecture_agent": _FakeAgent("architecture_agent", partial, interrupted=1),
              "report_agent": _FakeAgent("report_agent", "reporte")}
    monkeypatch.setattr(orch, "_get_agent", lambda name: agents[name])
    steps = [{"agent": "architecture_agent", "task": "analiza"}, {"agent": "report_agent", "task": "reporta"}]
    monkeypatch.setattr(planner, "_llm_plan", lambda *a, **k: planner.LLMPlan(steps=steps))
    out = orch.run("analiza el flujo de pagos y genera un reporte", use_llm_planner=True)
    assert agents["architecture_agent"].ran == 1
    assert agents["report_agent"].ran == 0, "el segundo paso no debe correr sobre un parcial"
    assert "reporte" not in out and orch.last_run_ok is False


def test_respuesta_vacia_en_orquestador_no_es_util(orch, monkeypatch):
    agents = {"architecture_agent": _FakeAgent("architecture_agent", EMPTY_RESPONSE, empty=1)}
    monkeypatch.setattr(orch, "_get_agent", lambda name: agents[name])
    out = orch.run("analiza el flujo", use_llm_planner=False, forced_agent="architecture_agent")
    assert out.strip() == EMPTY_RESPONSE and orch.last_run_ok is False and orch.memory.as_messages() == []
    assert orch.last_usage["totals"]["empty_response"] == 1


# ---------------------------------------------------------------------------
# 3c. métricas y `minka stats`
# ---------------------------------------------------------------------------

USAGE = {"agent": "architecture_agent", "model": "claude-haiku-4-5", "requests": 1, "input_tokens": 10, "output_tokens": 5,
         "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_5m": 0, "cache_creation_1h": 0}


def _record(interrupted=0, empty=0):
    totals = {k: USAGE[k] for k in metrics.TOKEN_KEYS}
    totals.update(interrupted=interrupted, empty_response=empty)
    return metrics.build_run_record(run_id="r1", source="cli", project="demo", cwd="/tmp", router="info_direct",
                                    plan=["architecture_agent"], fix_loop_iters=0, ok=not (interrupted or empty),
                                    duration_s=1.0, usage={"per_agent": [USAGE], "totals": totals})


def test_metricas_cuentan_interrumpidas_y_vacias():
    recs = [_record(interrupted=1), _record(empty=1), _record()]
    assert [r["interrupted"] for r in recs] == [1, 0, 0] and [r["empty_responses"] for r in recs] == [0, 1, 0]
    agg = metrics.aggregate(recs)
    assert agg["interrupted"] == 1 and agg["empty_responses"] == 1


def test_stats_muestra_interrumpidas(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(metrics, "read_run_records", lambda *a, **k: [_record(interrupted=1), _record()])
    res = _invoke(["stats"])
    assert res.exit_code == 0 and "interrumpidas: 1" in res.output and "respuestas vacías: 0" in res.output


# ---------------------------------------------------------------------------
# 3d. evals quality: el parcial no cuenta como acierto
# ---------------------------------------------------------------------------


def _git(path, *args):
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


def test_eval_quality_parcial_queda_no_evaluable(tmp_path):
    api = tmp_path / "svc-api"
    (api / "service").mkdir(parents=True)
    (api / "service" / "token.go").write_text("package service\n\nfunc ResolveToken() string {\n\treturn \"\"\n}\n")
    _git(api, "init", "-q", "-b", "main")
    _git(api, "config", "user.email", "t@example.com")
    _git(api, "config", "user.name", "t")
    _git(api, "add", "-A")
    _git(api, "commit", "-q", "-m", "init")
    repos = {"api": str(api)}
    case = EvalCase.model_validate({"id": "c1", "input": "¿dónde se resuelve el token?", "agent": "architecture_agent",
                                    "type": "location", "expect": {"file": "svc-api/service/token.go", "symbol": "ResolveToken"}})
    outputs = iter([f"svc-api/service/token.go:3\n\n{INTERRUPTED_PREFIX} timeout, 1 vueltas completadas.", EMPTY_RESPONSE])
    medians = {"info_direct": 0.10, "llm": 0.40, "forced": 0.20, "other": 0.20}
    report = qe.evaluate([case, case.model_copy(update={"id": "c2"})], repos, runs=1, max_usd=6,
                         runner=lambda c: (next(outputs), 0.05, "info_direct"), medians=medians)
    assert report["aciertos"] == 0 and report["no_evaluables"] == 2
    assert all(r["verdict"] == qe.VERDICT_NA for r in report["results"])


# ---------------------------------------------------------------------------
# 3e. CLI sale con 1 y una línea; el REPL sigue vivo
# ---------------------------------------------------------------------------


class _RaisingOrch:
    def __init__(self, exc, then="respuesta ok"):
        self.exc, self.then, self.calls = exc, then, 0
        self.last_run_ok = True
        self.last_usage = {"per_agent": [USAGE], "totals": USAGE}
        self.last_run_record = None
        self.memory = []

    def clear_session(self):
        pass

    def run(self, task, **kw):
        self.calls += 1
        if self.calls == 1:
            raise self.exc
        return self.then


@pytest.fixture
def cli(monkeypatch, tmp_path):
    monkeypatch.setattr(minka_main, "_require_llm_backend", lambda: None)
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(config, "AWS_PROFILE", "perfil-demo", raising=False)

    def use(orch):
        monkeypatch.setattr(minka_main, "_make_orchestrator", lambda: orch)
        return orch

    return use


def test_cli_sale_con_1_y_una_linea(cli):
    cli(_RaisingOrch(be.UnauthorizedSSOTokenError()))
    res = _invoke(["--no-interactive", "analiza el flujo"])
    assert res.exit_code == 1
    assert "Traceback" not in res.output
    assert f"{INTERRUPTED_PREFIX} credenciales" in res.output and "aws sso login --profile perfil-demo" in res.output


def test_repl_no_muere_e_imprime_una_linea(cli):
    orch = cli(_RaisingOrch(anthropic.APITimeoutError(request=_req())))
    res = _invoke([], input="analiza el flujo\nanaliza otra vez\nsalir\n")
    assert res.exit_code == 0 and orch.calls == 2
    assert f"{INTERRUPTED_PREFIX} timeout" in res.output and "respuesta ok" in res.output


def test_type_error_en_cli_no_se_oculta(cli):
    cli(_RaisingOrch(TypeError("bug")))
    res = _invoke(["--no-interactive", "analiza el flujo"])
    assert res.exit_code != 0 and isinstance(res.exception, TypeError)
