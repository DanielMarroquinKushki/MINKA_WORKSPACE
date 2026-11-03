"""planner_error: clase de fallo del planner LLM, reintento único ante transitorios y empty_plan."""

from types import SimpleNamespace

import pytest
from pydantic import BaseModel, ValidationError

from minka.core import structured
from minka.core.orchestrator import planner
from minka.core.structured import StructuredError, call_structured_result, classify_exception


class Demo(BaseModel):
    agent: str


class APITimeoutError(Exception):  # mismo nombre que la clase del SDK: se clasifica por nombre
    pass


def _tool_use(payload):
    block = SimpleNamespace(type="tool_use", name="submit_demo", input=payload)
    return SimpleNamespace(content=[block], stop_reason="tool_use")


class _Client:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0
        self.messages = self

    def create(self, **kw):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _call(client, retry=True):
    return call_structured_result(
        client, "m", "sys", [{"role": "user", "content": "x"}], Demo, "submit_demo", "d",
        retry_transient=retry,
    )


@pytest.fixture(autouse=True)
def sin_espera(monkeypatch):
    monkeypatch.setattr(structured, "RETRY_DELAY_S", 0)


def test_transient_reintenta_una_vez_y_sale_bien():
    client = _Client(APITimeoutError("timeout"), _tool_use({"agent": "a"}))
    result, error, retries, _ = _call(client)
    assert result.agent == "a" and error is None and retries == 1 and client.calls == 2


def test_transient_dos_veces_devuelve_el_error_sin_tercer_intento():
    client = _Client(APITimeoutError("t1"), APITimeoutError("t2"))
    result, error, retries, _ = _call(client)
    assert result is None and error.kind == "transient" and retries == 1 and client.calls == 2
    assert error.exc_class == "APITimeoutError" and error.message == "t2"


def test_api_no_reintenta():
    client = _Client(ValueError("x" * 500), _tool_use({"agent": "a"}))
    result, error, retries, _ = _call(client)
    assert result is None and error.kind == "api" and retries == 0 and client.calls == 1
    assert len(error.message) == 200


def test_sin_retry_transient_no_reintenta():
    client = _Client(APITimeoutError("t"), _tool_use({"agent": "a"}))
    result, error, retries, _ = _call(client, retry=False)
    assert result is None and error.kind == "transient" and client.calls == 1


def test_no_tool_block_y_validation():
    sin_bloque = SimpleNamespace(content=[SimpleNamespace(type="text", text="hola")], stop_reason="end_turn")
    _, error, _, _ = _call(_Client(sin_bloque))
    assert error.kind == "no_tool_block" and "end_turn" in error.message
    _, error, _, _ = _call(_Client(_tool_use({"otro": 1})))
    assert error.kind == "validation" and error.exc_class == "ValidationError"


def test_classify_por_status_code():
    exc = Exception("boom")
    exc.status_code = 503
    assert classify_exception(exc) == "transient"
    exc.status_code = 400
    assert classify_exception(exc) == "api"


def test_plan_with_router_empty_plan_no_cae_a_keywords(monkeypatch):
    monkeypatch.setattr(
        planner, "_llm_plan",
        lambda task, context="": planner.LLMPlan(None, StructuredError("empty_plan", "", "sin pasos"), "nada"),
    )
    result = planner.plan_with_router("corrige el handler", use_llm=True)
    assert result.router == "no_plan" and result.steps == []
    assert result.planner_error["kind"] == "empty_plan" and result.reasoning == "nada"
    # make_plan (compat) sí cae a keywords
    assert planner.make_plan("corrige el handler")[0]["agent"] == "development_agent"


def test_plan_with_router_error_cae_a_keywords_con_el_error(monkeypatch):
    monkeypatch.setattr(
        planner, "_llm_plan",
        lambda task, context="": planner.LLMPlan(None, StructuredError("transient", "RateLimitError", "429"), None, 1),
    )
    result = planner.plan_with_router("analiza riesgos", use_llm=True)
    assert result.router == "keyword" and result.steps[0]["agent"] == "architecture_agent"
    assert result.planner_error == {"kind": "transient", "class": "RateLimitError", "message": "429"}
    assert result.retries == 1


def test_schema_plan_acepta_reasoning_opcional():
    from minka.core.schemas import Plan

    paso = {"agent": "architecture_agent", "task": "t"}
    assert Plan.model_validate({"steps": [paso]}).reasoning is None
    plan = Plan.model_validate({"steps": [paso], "reasoning": "nada que hacer"})
    assert plan.reasoning == "nada que hacer"
    assert "reasoning" in Plan.model_json_schema()["properties"]


def test_validation_se_reintenta_una_vez_si_se_pide():
    client = _Client(_tool_use({"otro": 1}), _tool_use({"agent": "a"}))
    result, error, retries, usage = call_structured_result(
        client, "m", "sys", [{"role": "user", "content": "x"}], Demo, "submit_demo", "d",
        retry_validation=True,
    )
    assert result.agent == "a" and error is None and retries == 1 and client.calls == 2
    assert usage["requests"] == 2
    client = _Client(_tool_use({"otro": 1}), _tool_use({"otro": 2}))
    result, error, retries, _ = call_structured_result(
        client, "m", "sys", [{"role": "user", "content": "x"}], Demo, "submit_demo", "d",
        retry_validation=True,
    )
    assert result is None and error.kind == "validation" and retries == 1 and client.calls == 2


def test_usage_acumula_tokens_de_la_respuesta():
    resp = _tool_use({"agent": "a"})
    resp.usage = SimpleNamespace(input_tokens=100, output_tokens=7, cache_read_input_tokens=50,
                                 cache_creation_input_tokens=0)
    _, _, _, usage = _call(_Client(resp))
    assert usage["input_tokens"] == 100 and usage["output_tokens"] == 7
    assert usage["cache_read_input_tokens"] == 50 and usage["model"] == "m"


def test_plan_with_router_validation_cae_a_no_plan(monkeypatch):
    monkeypatch.setattr(
        planner, "_llm_plan",
        lambda task, context="": planner.LLMPlan(None, StructuredError("validation", "ValidationError", "agent inventado"), None, 1),
    )
    result = planner.plan_with_router("¿dónde se resuelve el token?", use_llm=True)
    assert result.router == "no_plan" and result.steps == [] and result.retries == 1
    assert result.planner_error["kind"] == "validation"


def test_schema_plan_pistas_nuevas():
    from minka.core.schemas import Plan

    props = Plan.model_json_schema()["properties"]
    assert props["steps"]["description"] == "Al menos un paso; si no es una tarea concreta, architecture_agent."
    assert props["steps"]["minItems"] == 1  # steps=[] ya no valida: cae a la red de seguridad
    assert "máximo 6 palabras" in props["reasoning"]["description"]
    assert "reasoning" not in Plan.model_json_schema().get("required", [])  # sigue opcional
    paso = {"agent": "architecture_agent", "task": "t"}
    assert Plan.model_validate({"steps": [paso]}).reasoning is None
    with pytest.raises(ValidationError):
        Plan.model_validate({"steps": []})


def test_schema_de_la_tool_sin_titulo_ni_descripcion_de_nivel_superior():
    from minka.core.schemas import Plan
    from minka.core.structured import tool_input_schema

    schema = tool_input_schema(Plan)
    assert "title" not in schema and "description" not in schema
    assert schema["properties"]["steps"]["minItems"] == 1  # el resto del schema intacto
    assert "description" in Plan.model_json_schema()  # el modelo Pydantic no cambia


def test_respuesta_anidada_en_una_clave_se_desenvuelve_sin_llamada_extra():
    from minka.core.structured import unwrap

    assert unwrap({"plan": {"agent": "a"}}) == {"agent": "a"}
    assert unwrap({"agent": "a"}) == {"agent": "a"}
    assert unwrap({"a": 1, "b": 2}) == {"a": 1, "b": 2}
    client = _Client(_tool_use({"demo": {"agent": "a"}}))
    result, error, retries, _ = _call(client)
    assert result.agent == "a" and error is None and retries == 0 and client.calls == 1
