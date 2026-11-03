"""
structured — salidas estructuradas del LLM vía tool-use forzado.

Reemplaza el parsing frágil (regex sobre JSON, scraping de strings) por una
llamada donde el modelo DEBE responder con la forma de un modelo Pydantic: se
expone el schema como tool, se fuerza su uso (tool_choice) y se valida el input
resultante con Pydantic. Funciona igual en Bedrock y API directa (por eso se usa
tool-use y no los native structured outputs, que tienen gotchas en Bedrock).

Convención del proyecto: nunca lanza hacia el caller. `call_structured` devuelve
None ante cualquier fallo; `call_structured_result` además dice POR QUÉ
(StructuredError.kind) y reintenta una vez los errores transitorios si se pide.
"""

import time
from dataclasses import dataclass
from typing import TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)

# Errores de red/cuota que suelen pasar al reintentar. Por nombre de clase para no
# importar anthropic ni botocore aquí; APIStatusError 429/5xx también cuenta.
TRANSIENT_CLASSES = frozenset(
    {
        "APIConnectionError",
        "APITimeoutError",
        "RateLimitError",
        "InternalServerError",
        "ReadTimeoutError",
        "ConnectTimeoutError",
        "EndpointConnectionError",
        "ServiceUnavailableError",
        "ThrottlingException",
    }
)
RETRY_DELAY_S = 1.0


@dataclass
class StructuredError:
    kind: str  # transient | api | no_tool_block | validation | empty_plan
    exc_class: str
    message: str

    def as_dict(self) -> dict:
        return {"kind": self.kind, "class": self.exc_class, "message": self.message}


def classify_exception(exc: Exception) -> str:
    status = getattr(exc, "status_code", None)
    if type(exc).__name__ in TRANSIENT_CLASSES:
        return "transient"
    if isinstance(status, int) and (status == 429 or status >= 500):
        return "transient"
    return "api"


def call_structured_result(
    client,
    model: str,
    system: str,
    messages: list[dict],
    schema_model: type[T],
    tool_name: str,
    tool_description: str,
    max_tokens: int = 1024,
    retry_transient: bool = False,
) -> tuple[T | None, StructuredError | None, int]:
    """(instancia validada | None, error | None, reintentos hechos).

    Con retry_transient, un error transitorio se reintenta UNA vez tras
    RETRY_DELAY_S; el resto de errores no se reintenta.
    """
    tool = {
        "name": tool_name,
        "description": tool_description,
        "input_schema": schema_model.model_json_schema(),
    }
    retries = 0
    while True:
        try:
            response = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                tools=[tool],
                tool_choice={"type": "tool", "name": tool_name},
                messages=messages,
            )
            break
        except Exception as exc:  # noqa: BLE001 — se clasifica, nunca se relanza
            kind = classify_exception(exc)
            if kind == "transient" and retry_transient and retries == 0:
                retries += 1
                time.sleep(RETRY_DELAY_S)
                continue
            return None, StructuredError(kind, type(exc).__name__, str(exc)[:200]), retries

    for block in getattr(response, "content", []):
        if getattr(block, "type", None) == "tool_use" and block.name == tool_name:
            try:
                return schema_model.model_validate(block.input), None, retries
            except ValidationError as exc:
                return None, StructuredError("validation", "ValidationError", str(exc)[:200]), retries
    stop = getattr(response, "stop_reason", None)
    return None, StructuredError("no_tool_block", "", f"sin bloque {tool_name}; stop_reason={stop}"), retries


def call_structured(
    client,
    model: str,
    system: str,
    messages: list[dict],
    schema_model: type[T],
    tool_name: str,
    tool_description: str,
    max_tokens: int = 1024,
) -> T | None:
    """Instancia validada de `schema_model`, o None si algo falla (ver call_structured_result)."""
    return call_structured_result(
        client, model, system, messages, schema_model, tool_name, tool_description, max_tokens
    )[0]
