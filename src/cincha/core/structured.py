"""
structured — salidas estructuradas del LLM vía tool-use forzado.

Reemplaza el parsing frágil (regex sobre JSON, scraping de strings) por una
llamada donde el modelo DEBE responder con la forma de un modelo Pydantic: se
expone el schema como tool, se fuerza su uso (tool_choice) y se valida el input
resultante con Pydantic. Funciona igual en Bedrock y API directa (por eso se usa
tool-use y no los native structured outputs, que tienen gotchas en Bedrock).

Convención del proyecto: nunca lanza hacia el caller — ante cualquier fallo
(red, sin bloque tool_use, validación) devuelve None y el caller decide el
fallback (p.ej. el planner cae a routing por keywords).
"""

from typing import TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)


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
    """
    Pide al LLM una respuesta con la forma de `schema_model` vía tool-use forzado.

    Args:
        client: cliente Anthropic (Bedrock o API directa), ya construido.
        model: model ID ya resuelto para el backend (ver resolve_model_id).
        system: system prompt.
        messages: historial en formato Messages API.
        schema_model: modelo Pydantic que define la forma de la respuesta.
        tool_name / tool_description: identidad de la tool expuesta al modelo.
        max_tokens: tope de tokens de la respuesta.

    Returns:
        Instancia validada de `schema_model`, o None si algo falla.
    """
    tool = {
        "name": tool_name,
        "description": tool_description,
        "input_schema": schema_model.model_json_schema(),
    }
    try:
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            tools=[tool],
            tool_choice={"type": "tool", "name": tool_name},
            messages=messages,
        )
    except Exception:  # noqa: BLE001 — fallo de infra/red: el caller usa su fallback
        return None

    # Busca el bloque tool_use con nuestro nombre y valida su input con Pydantic.
    for block in getattr(response, "content", []):
        if getattr(block, "type", None) == "tool_use" and block.name == tool_name:
            try:
                return schema_model.model_validate(block.input)
            except ValidationError:
                return None
    return None
